import collections
import numpy as np
import json
import openml
import openmlpimp
import operator
import os
import pickle
import warnings
from scipy.stats import gaussian_kde, rv_discrete, uniform

from collections import OrderedDict

from ConfigSpace.hyperparameters import CategoricalHyperparameter, NumericalHyperparameter, UniformFloatHyperparameter, UniformIntegerHyperparameter

from openmlstudy14.distributions import loguniform, loguniform_int


class rv_discrete_wrapper(object):
    def __init__(self, param_name, X):
        self.param_name = param_name
        self.X_prime = OrderedDict()
        for value in X:
            if value not in self.X_prime:
                self.X_prime[value] = 0
            self.X_prime[value] += (1.0 / len(X))
        self.distrib = rv_discrete(values=(list(range(len(self.X_prime))), list(self.X_prime.values())))

    @staticmethod
    def _is_castable_to(value, type):
        try:
            type(value)
            return True
        except ValueError:
            return False

    def rvs(self, *args, **kwargs):
        # assumes a samplesize of 1, for random search
        sample = self.distrib.rvs()
        value = list(self.X_prime.keys())[sample]
        if value in ['True', 'False']:
            return bool(value)
        elif self._is_castable_to(value, int):
            return int(value)
        elif self._is_castable_to(value, float):
            return float(value)
        else:
            return str(value)


class gaussian_kde_wrapper(object):
    def __init__(self, hyperparameter, param_name, X, log):
        self.param_name = param_name
        self.log = log
        self.const = False
        self.hyperparameter = hyperparameter
        if self.log:
            self.distrib = gaussian_kde(np.log2(X))
            if isinstance(self.hyperparameter, UniformIntegerHyperparameter):
                self.probabilities = {val: self.distrib.pdf(np.log2(val)) for val in range(self.hyperparameter.lower, self.hyperparameter.upper + 1)}
        else:
            self.distrib = gaussian_kde(X)
            if isinstance(self.hyperparameter, UniformIntegerHyperparameter):
                self.probabilities = {val: self.distrib.pdf(val)[0] for val in range(self.hyperparameter.lower, self.hyperparameter.upper + 1)}

                # normalize the dict
                factor = 1.0 / sum(self.probabilities.values())
                for k in self.probabilities:
                    self.probabilities[k] = self.probabilities[k] * factor

    def pdf(self, x):
        if self.const:
            raise ValueError('No pdf available for constant value')
        if self.log:
            return self.distrib.pdf(np.log2(x))
        else:
            return self.distrib.pdf(x)

    def rvs(self, *args, **kwargs):
        # assumes a samplesize of 1, for random search
        if isinstance(self.hyperparameter, UniformIntegerHyperparameter):
            values = list(self.probabilities.keys())
            probs = list(self.probabilities.values())
            # cast to int, because of np serializability
            return int(np.random.choice(a=values, p=probs))
        while True:
            sample = self.distrib.resample(size=1)[0][0]
            if self.log:
                value = np.power(2, sample)
            else:
                value = sample

            if self.hyperparameter.lower <= value <= self.hyperparameter.upper:
                return value


def cache_priors(cache_directory, study_id, flow_id, fixed_parameters):
    study = openml.study.get_study(study_id, 'tasks')
    setups = openmlpimp.utils.obtain_all_setups(flow=flow_id)

    task_setup_scores = collections.defaultdict(dict)
    for task_id in study.tasks:
        print("task", task_id)
        runs = openml.evaluations.list_evaluations("predictive_accuracy", task=[task_id], flow=[flow_id])
        for run in runs.values():
            if openmlpimp.utils.setup_complies_to_fixed_parameters(setups[run.setup_id], 'parameter_name', fixed_parameters):
                task_setup_scores[task_id][run.setup_id] = run.value

                # if len(best_setupids) > 10: break
    try:
        os.makedirs(cache_directory)
    except FileExistsError:
        pass

    with open(cache_directory + '/best_setup_per_task.pkl', 'wb') as f:
        pickle.dump(task_setup_scores, f, pickle.HIGHEST_PROTOCOL)


def obtain_priors(cache_directory, study_id, flow_id, hyperparameters, fixed_parameters, holdout, bestN):
    """
    Obtains the priors based on (almost) all tasks in an OpenML study

    Parameters
    -------
    cache_directory : str
        a directory on the filesystem to store and obtain the cache from

    study_id : int
        the study id to obtain the priors from

    flow id : int
        the flow id of the classifier

    hyperparameters : dict[str, ConfigSpace.Hyperparameter]
        dictionary mapping from parameter name to the ConfigSpace Hyperparameter object

    fixed_parameters : dict[str, str]
        maps from hyperparameter name to a value. Only setups are considered
        that have this hyperparameter set to this specific value

    holdout : list[int]
        OpenML task id to not involve in the sampling

    bestN : int
        from each task, take the N best setups.

    Returns
    -------
    X : dict[str, list[mixed]]
        Mapping from hyperparameter name to a list of the best values.
    """
    filename = cache_directory + '/best_setup_per_task.pkl'
    if not os.path.isfile(filename):
        print(filename)
        print('%s No cache file for setups, will create one ... ' %openmlpimp.utils.get_time())
        cache_priors(cache_directory, study_id, flow_id, fixed_parameters)
        print('%s Cache created. Available in: %s' %(openmlpimp.utils.get_time(),filename))

    with open(filename, 'rb') as f:
        task_setup_scores = pickle.load(f)
    task_setups = dict()
    all_setups = set()
    for task, setup_scores in task_setup_scores.items():
        if len(setup_scores) < bestN * 4:
            warnings.warn('Not enough setups for task %d. Need %d, expected at least %d, got %d' %(task, bestN, bestN*2, len(setup_scores)))
        task_setups[task] = dict(sorted(setup_scores.items(), key=operator.itemgetter(1), reverse=True)[:bestN]).keys()
        all_setups |= set(task_setups[task])

    X = {parameter: list() for parameter in hyperparameters.keys()}
    setups = openmlpimp.utils.obtain_setups_by_setup_id(setup_ids=list(all_setups), flow=flow_id)

    for task_id, best_setups in task_setups.items():
        if task_id in holdout:
            print('Holdout task %d' %task_id)
            continue

        for setup_id in best_setups:
            paramname_paramidx = {param.parameter_name: idx for idx, param in setups[setup_id].parameters.items()}
            for param_name, parameter in hyperparameters.items():
                param = setups[setup_id].parameters[paramname_paramidx[param_name]]
                if isinstance(parameter, NumericalHyperparameter):
                    X[param_name].append(float(param.value))
                elif isinstance(parameter, CategoricalHyperparameter):
                    X[param_name].append(json.loads(param.value))
                else:
                    raise ValueError()

    for parameter in X:
        X[parameter] = np.array(X[parameter])
    return X


def get_prior_paramgrid(cache_directory, study_id, flow_id, hyperparameters, fixed_parameters, holdout=None, bestN=1):
    priors = obtain_priors(cache_directory, study_id, flow_id, hyperparameters, fixed_parameters, holdout, bestN)
    param_grid = dict()

    for parameter_name, prior in priors.items():
        if fixed_parameters is not None and parameter_name in fixed_parameters.keys():
            continue
        if all(x == prior[0] for x in prior):
            warnings.warn('Skipping Hyperparameter %s: All prior values equals (%s). ' %(parameter_name, prior[0]))
            continue
        hyperparameter = hyperparameters[parameter_name]
        if isinstance(hyperparameter, CategoricalHyperparameter):
            param_grid[parameter_name] = rv_discrete_wrapper(parameter_name, prior)
        elif isinstance(hyperparameter, NumericalHyperparameter):
            param_grid[parameter_name] = gaussian_kde_wrapper(hyperparameter, parameter_name, prior, hyperparameter.log)
        else:
            raise ValueError()
    return param_grid


def get_uniform_paramgrid(hyperparameters, fixed_parameters):
    param_grid = dict()
    for param_name, hyperparameter in hyperparameters.items():
        if fixed_parameters is not None and param_name in fixed_parameters.keys():
            continue
        if isinstance(hyperparameter, CategoricalHyperparameter):
            all_values = hyperparameter.choices
            if all(item in ['True', 'False'] for item in all_values):
                all_values = [bool(item) for item in all_values]
            param_grid[param_name] = all_values
        elif isinstance(hyperparameter, UniformFloatHyperparameter):
            if hyperparameter.log:
                param_grid[param_name] = loguniform(base=2, low=hyperparameter.lower, high=hyperparameter.upper)
            else:

                param_grid[param_name] = uniform(loc=hyperparameter.lower, scale=hyperparameter.upper)
        elif isinstance(hyperparameter, UniformIntegerHyperparameter):
            if hyperparameter.log:
                param_grid[param_name] = loguniform_int(base=2, low=hyperparameter.lower, high=hyperparameter.upper)
            else:
                param_grid[param_name] = list(range(hyperparameter.lower, hyperparameter.upper))
        else:
            raise ValueError()
    return param_grid
