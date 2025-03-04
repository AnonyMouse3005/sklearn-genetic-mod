# sklearn-genetic - Genetic feature selection module for scikit-learn
# Copyright (C) 2016-2022  Manuel Calzolari
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Genetic algorithm for feature selection"""

import numbers
import multiprocess
import itertools
import numpy as np
from sklearn.utils import check_X_y
from sklearn.utils.metaestimators import if_delegate_has_method
from sklearn.base import BaseEstimator
from sklearn.base import MetaEstimatorMixin
from sklearn.base import clone
from sklearn.base import is_classifier
from sklearn.model_selection import check_cv, cross_val_score
from sklearn.metrics import check_scoring
from sklearn.feature_selection import SelectorMixin
from sklearn.utils._joblib import cpu_count
from sklearn.utils._testing import ignore_warnings
from sklearn.exceptions import ConvergenceWarning
from deap import algorithms
from deap import base
from deap import creator
from deap import tools


#@ specify 3 objectives: (1) mean CV ACC (maximize), (2) num. of features (minimize), (3) std of CV ACC's (minimize)
#@ the magnitude of the weight is used to vary the importance of each objective one against another (here all 1's mean that all 3 objs are equally important)
creator.create("Fitness_new", base.Fitness, weights=(1.0, -0.1, -0.5))
creator.create("Individual_new", list, fitness=creator.Fitness_new)


def _eaFunction(population, toolbox, cxpb, mutpb, ngen, ngen_no_change=None, stats=None,
                halloffame=None, verbose=0, hparams=None, hparam_bits=0):
    logbook = tools.Logbook()
    logbook.header = ['gen', 'nevals'] + (stats.fields if stats else [])

    # Evaluate the individuals with an invalid fitness
    invalid_ind = [ind for ind in population if not ind.fitness.valid]
    fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)  #@ apply _evalFunction() on each of the ind in invalid_ind
    for ind, fit in zip(invalid_ind, fitnesses):
        ind.fitness.values = fit

    if halloffame is None:
        raise ValueError("The 'halloffame' parameter should not be None.")

    halloffame.update(population)
    hof_size = len(halloffame.items) if halloffame.items else 0

    record = stats.compile(population) if stats else {}
    logbook.record(gen=0, nevals=len(invalid_ind), **record)
    if verbose:
        print(logbook.stream)

    # Begin the generational process
    wait = 0
    for gen in range(1, ngen + 1):
        # Select the next generation individuals
        offspring = toolbox.select(population, len(population) - hof_size)

        # Vary the pool of individuals
        if hparams:  # split the gene of each ind in offspring into [param 1's gene] + [param 2's gene] + ... + [gene for features] and variate each separately
            n_pbits = hparams['bitwidth']
            genes = []
            i = 0
            for _ in range(len(hparams['names'])):
                offspring_h = [creator.Individual_new(ind[i:i+n_pbits]) for ind in offspring]
                genes.append(algorithms.varAnd(offspring_h, toolbox, cxpb, mutpb))
                i += n_pbits
            offspring_f = [creator.Individual_new(ind[i:]) for ind in offspring]
            genes.append(algorithms.varAnd(offspring_f, toolbox, cxpb, mutpb))
            offspring = [creator.Individual_new(itertools.chain.from_iterable(ind)) for ind in zip(*genes)]
        else:
            offspring = algorithms.varAnd(offspring, toolbox, cxpb, mutpb)

        # Evaluate the individuals with an invalid fitness (i.e., the modified individuals)
        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit

        # Add the best back to population
        offspring.extend(halloffame.items)

        # Get the previous best individual before updating the hall of fame
        prev_best = halloffame[0]

        # Update the hall of fame with the generated individuals
        halloffame.update(offspring)

        # Replace the current population by the offspring
        population[:] = offspring

        # Append the current generation statistics to the logbook
        record = stats.compile(population) if stats else {}
        logbook.record(gen=gen, nevals=len(invalid_ind), **record)
        if verbose:
            print(logbook.stream)

        # If the new best individual is the same as the previous best individual,
        # increment a counter, otherwise reset the counter
        if halloffame[0] == prev_best:
            wait += 1
        else:
            wait = 0

        # If the counter reached the termination criteria, stop the optimization
        if ngen_no_change is not None and wait >= ngen_no_change:
            break

    return population, logbook


def _createIndividual(icls, n, max_features, hparams, hparam_bits):  #@ icls: class for individual (here is a list)
    n_features = np.random.randint(1, max_features + 1)
    f_genome = ([1] * n_features) + ([0] * (n - n_features))
    np.random.shuffle(f_genome)
    if hparams:
        n_1 = np.random.randint(0, hparam_bits + 1)
        h_genome = ([1] * n_1) + ([0] * (hparam_bits - n_1))
        # h_genome = [0] * hparam_bits
        np.random.shuffle(h_genome)
        return icls(h_genome + f_genome)
    else:   return icls(f_genome)


#@ Fitness function. Mod this to also include selecting hyperparams in GA
@ignore_warnings(category=ConvergenceWarning)
def _evalFunction(individual, estimator, X, y, groups, cv, scorer, fit_params, max_features, hparams,
                  caching, scores_cache={}):
    if hparams:  #@ WHY this block causes a decrease in performance, even when hparams is None???
        #@ extract info of hparams from individual's bit string
        n_pbits = hparams['bitwidth']
        i = 0
        for j, hparam in enumerate(hparams['names']):
            bin_str = individual[i:i+n_pbits]  # genotype
            dec_val = int(''.join(str(b) for b in bin_str), 2)  # decimal value of bin string
            p_min, p_max = hparams['range'][j][0], hparams['range'][j][1]
            p = p_min + dec_val*((p_max-p_min)/(2**n_pbits-1))  # actual value of param (phenotype)
            if hparam in ['max_depth']:      p = int(round(p, 0))
            setattr(estimator, hparam, p)
            i += n_pbits
        individual = individual[i:]
    individual_sum = np.sum(individual, axis=0)
    if individual_sum == 0 or individual_sum > max_features:
        return -10000, individual_sum, 10000
    individual_tuple = tuple(individual)
    if caching and individual_tuple in scores_cache:
        return scores_cache[individual_tuple][0], individual_sum, scores_cache[individual_tuple][1]
    X_selected = X[:, np.array(individual, dtype=np.bool)]
    scores = cross_val_score(estimator=estimator, X=X_selected, y=y, groups=groups, scoring=scorer,
                             cv=cv, fit_params=fit_params)
    scores_mean = np.mean(scores)
    scores_std = np.std(scores)
    if caching:
        scores_cache[individual_tuple] = [scores_mean, scores_std]
    return scores_mean, individual_sum, scores_std  #@ multi-objective fitness function


class GeneticSelectionCV_mod(BaseEstimator, MetaEstimatorMixin, SelectorMixin):
    """Feature selection with genetic algorithm.

    Parameters
    ----------
    estimator : object
        A supervised learning estimator with a `fit` method.

    cv : int, cross-validation generator or an iterable, optional
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - An object to be used as a cross-validation generator.
        - An iterable yielding train/test splits.

        For integer/None inputs, if ``y`` is binary or multiclass,
        :class:`StratifiedKFold` used. If the estimator is a classifier
        or if ``y`` is neither binary nor multiclass, :class:`KFold` is used.

    scoring : string, callable or None, optional, default: None
        A string (see model evaluation documentation) or
        a scorer callable object / function with signature
        ``scorer(estimator, X, y)``.

    fit_params : dict, optional
        Parameters to pass to the fit method.

    max_features : int or None, optional
        The maximum number of features selected.

    verbose : int, default=0
        Controls verbosity of output.

    n_jobs : int, default 1
        Number of cores to run in parallel.
        Defaults to 1 core. If `n_jobs=-1`, then number of jobs is set
        to number of cores.

    n_population : int, default=300
        Number of population for the genetic algorithm.

    crossover_proba : float, default=0.5
        Probability of crossover for the genetic algorithm.

    mutation_proba : float, default=0.2
        Probability of mutation for the genetic algorithm.

    n_generations : int, default=40
        Number of generations for the genetic algorithm.

    crossover_independent_proba : float, default=0.1
        Independent probability for each attribute to be exchanged, for the genetic algorithm.

    mutation_independent_proba : float, default=0.05
        Independent probability for each attribute to be mutated, for the genetic algorithm.

    tournament_size : int, default=3
        Tournament size for the genetic algorithm.

    n_gen_no_change : int, default None
        If set to a number, it will terminate optimization when best individual is not
        changing in all of the previous ``n_gen_no_change`` number of generations.

    caching : boolean, default=False
        If True, scores of the genetic algorithm are cached.

    Attributes
    ----------
    n_features_ : int
        The number of selected features with cross-validation.

    support_ : array of shape [n_features]
        The mask of selected features.

    generation_scores_ : array of shape [n_generations]
        The maximum cross-validation score for each generation.

    estimator_ : object
        The external estimator fit on the reduced dataset.

    Examples
    --------
    An example showing genetic feature selection.

    >>> import numpy as np
    >>> from sklearn import datasets, linear_model
    >>> from genetic_selection import GeneticSelectionCV
    >>> iris = datasets.load_iris()
    >>> E = np.random.uniform(0, 0.1, size=(len(iris.data), 20))
    >>> X = np.hstack((iris.data, E))
    >>> y = iris.target
    >>> estimator = linear_model.LogisticRegression(solver="liblinear", multi_class="ovr")
    >>> selector = GeneticSelectionCV(estimator, cv=5)
    >>> selector = selector.fit(X, y)
    >>> selector.support_ # doctest: +NORMALIZE_WHITESPACE
    array([ True  True  True  True False False False False False False False False
           False False False False False False False False False False False False], dtype=bool)
    """
    def __init__(self, estimator, cv=None, scoring=None, fit_params=None, max_features=None,
                 verbose=0, n_jobs=1, n_population=300, crossover_proba=0.5, mutation_proba=0.2,
                 n_generations=40, crossover_independent_proba=0.1,
                 mutation_independent_proba=0.05, tournament_size=3, n_gen_no_change=None, hparams=None,
                 caching=False):
        self.estimator = estimator
        self.cv = cv
        self.scoring = scoring
        self.fit_params = fit_params
        self.max_features = max_features
        self.verbose = verbose
        self.n_jobs = n_jobs
        self.n_population = n_population
        self.crossover_proba = crossover_proba
        self.mutation_proba = mutation_proba
        self.n_generations = n_generations
        self.crossover_independent_proba = crossover_independent_proba
        self.mutation_independent_proba = mutation_independent_proba
        self.tournament_size = tournament_size
        self.n_gen_no_change = n_gen_no_change
        self.hparams = hparams
        self.hparam_bits = len(self.hparams['names'])*self.hparams['bitwidth'] if self.hparams else 0  #@ number of bits for all hyperparameters to be tuned
        self.caching = caching
        self.scores_cache = {}

    @property
    def _estimator_type(self):
        return self.estimator._estimator_type

    def fit(self, X, y, groups=None):
        """Fit the GeneticSelectionCV model and the underlying estimator on the selected features.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            The training input samples.

        y : array-like, shape = [n_samples]
            The target values.

        groups : array-like, shape = [n_samples], optional
            Group labels for the samples used while splitting the dataset into
            train/test set. Only used in conjunction with a "Group" `cv`
            instance (e.g., `GroupKFold`).
        """
        return self._fit(X, y, groups)

    def _fit(self, X, y, groups=None):
        X, y = check_X_y(X, y, "csr")
        # Initialization
        cv = check_cv(self.cv, y, classifier=is_classifier(self.estimator))
        scorer = check_scoring(self.estimator, scoring=self.scoring)
        n_features = X.shape[1]

        if self.max_features is not None:
            if not isinstance(self.max_features, numbers.Integral):
                raise TypeError("'max_features' should be an integer between 1 and {} features."
                                " Got {!r} instead."
                                .format(n_features, self.max_features))
            elif self.max_features < 1 or self.max_features > n_features:
                raise ValueError("'max_features' should be between 1 and {} features."
                                 " Got {} instead."
                                 .format(n_features, self.max_features))
            max_features = self.max_features
        else:
            max_features = n_features

        if not isinstance(self.n_gen_no_change, (numbers.Integral, np.integer, type(None))):
            raise ValueError("'n_gen_no_change' should either be None or an integer."
                             " {} was passed."
                             .format(self.n_gen_no_change))

        estimator = clone(self.estimator)

        # Genetic Algorithm
        toolbox = base.Toolbox()

        toolbox.register("individual", _createIndividual, creator.Individual_new, n=n_features,
                         max_features=max_features, hparams=self.hparams, hparam_bits=self.hparam_bits)
        toolbox.register("population", tools.initRepeat, list, toolbox.individual)
        toolbox.register("evaluate", _evalFunction, estimator=estimator, X=X, y=y,
                         groups=groups, cv=cv, scorer=scorer, fit_params=self.fit_params,
                         max_features=max_features, hparams=self.hparams, caching=self.caching,
                         scores_cache=self.scores_cache)
        toolbox.register("mate", tools.cxUniform, indpb=self.crossover_independent_proba)
        toolbox.register("mutate", tools.mutFlipBit, indpb=self.mutation_independent_proba)
        toolbox.register("select", tools.selTournament, tournsize=self.tournament_size)

        if self.n_jobs == 0:
            raise ValueError("n_jobs == 0 has no meaning.")
        elif self.n_jobs > 1:
            pool = multiprocess.Pool(processes=self.n_jobs)
            toolbox.register("map", pool.map)
        elif self.n_jobs < 0:
            pool = multiprocess.Pool(processes=max(cpu_count() + 1 + self.n_jobs, 1))
            toolbox.register("map", pool.map)

        pop = toolbox.population(n=self.n_population)
        hof = tools.HallOfFame(1, similar=np.array_equal)
        stats = tools.Statistics(lambda ind: ind.fitness.values)
        stats.register("avg", np.mean, axis=0)
        stats.register("std", np.std, axis=0)
        stats.register("min", np.min, axis=0)
        stats.register("max", np.max, axis=0)

        if self.verbose > 0:
            print("Selecting features with genetic algorithm.")

        with np.printoptions(precision=6, suppress=True, sign=" "):
            _, log = _eaFunction(pop, toolbox, cxpb=self.crossover_proba,
                                 mutpb=self.mutation_proba, ngen=self.n_generations,
                                 ngen_no_change=self.n_gen_no_change,
                                 stats=stats, halloffame=hof, verbose=self.verbose, hparams=self.hparams, hparam_bits=self.hparam_bits)
        if self.n_jobs != 1:
            pool.close()
            pool.join()

        # Set final attributes
        if self.hparams:
            support_ = np.array(hof, dtype=np.bool)[0][self.hparam_bits:]
            n_pbits = self.hparams['bitwidth']
            i = 0
            best_params_binstr = hof[0][:self.hparam_bits]
            best_params_ = {}
            for j, hparam in enumerate(self.hparams['names']):
                bin_str = best_params_binstr[i:i+n_pbits]  # genotype
                dec_val = int(''.join(str(b) for b in bin_str), 2)  # decimal value of bin string
                p_min, p_max = self.hparams['range'][j][0], self.hparams['range'][j][1]
                p = p_min + dec_val*((p_max-p_min)/(2**n_pbits-1))  # actual value of param (phenotype)
                best_params_[hparam] = p
                i += n_pbits
            self.best_params_ = best_params_
        else:
            support_ = np.array(hof, dtype=np.bool)[0]
            self.best_params_ = self.estimator.get_params()
        self.estimator_ = clone(self.estimator)
        self.estimator_.fit(X[:, support_], y)

        self.generation_scores_ = np.array([score for score, _, _ in log.select("max")])
        self.n_features_ = support_.sum()
        self.support_ = support_

        return self

    @if_delegate_has_method(delegate='estimator')
    def predict(self, X):
        """Reduce X to the selected features and then predict using the underlying estimator.

        Parameters
        ----------
        X : array of shape [n_samples, n_features]
            The input samples.

        Returns
        -------
        y : array of shape [n_samples]
            The predicted target values.
        """
        return self.estimator_.predict(self.transform(X))

    @if_delegate_has_method(delegate='estimator')
    def score(self, X, y):
        """Reduce X to the selected features and return the score of the underlying estimator.

        Parameters
        ----------
        X : array of shape [n_samples, n_features]
            The input samples.

        y : array of shape [n_samples]
            The target values.
        """
        return self.estimator_.score(self.transform(X), y)

    def _get_support_mask(self):
        return self.support_

    @if_delegate_has_method(delegate='estimator')
    def decision_function(self, X):
        return self.estimator_.decision_function(self.transform(X))

    @if_delegate_has_method(delegate='estimator')
    def predict_proba(self, X):
        return self.estimator_.predict_proba(self.transform(X))

    @if_delegate_has_method(delegate='estimator')
    def predict_log_proba(self, X):
        return self.estimator_.predict_log_proba(self.transform(X))
