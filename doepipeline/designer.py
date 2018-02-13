import logging
import pyDOE2
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from collections import OrderedDict, namedtuple
from itertools import combinations_with_replacement


class OptimizationResult(namedtuple(
    'OptimizationResult', ['predicted_optimum', 'converged', 'tol'])):
    """ `namedtuple` encapsulating results from optimization. """


class UnsupportedFactorType(Exception):
    pass


class UnsupportedDesign(Exception):
    pass


class OptimizationFailed(Exception):
    pass


class NumericFactor:

    """ Base class for numeric factors.

    Simple class which encapsulates current settings and allowed
    max and min.

    Can't be instantiated.
    """
    type = None

    def __init__(self, factor_max, factor_min, current_low=None, current_high=None):
        if type(self) == NumericFactor:
            raise TypeError('NumericFactor can not be instantiated. Use '
                            'sub-classes instead.')
        self.current_low = current_low
        self.current_high = current_high
        self.max = factor_max
        self.min = factor_min

    @property
    def span(self):
        """ Distance between current high and low. """
        return self.current_high - self.current_low

    @property
    def center(self):
        """ Mean value of current high and low. """
        return (self.current_high + self.current_low) / 2.0

    def __repr__(self):
        return ('{}(factor_max={}, factor_min={}, current_low={}, '
                'current_high={})').format(self.__class__.__name__,
                                           self.max,
                                           self.min,
                                           self.current_low,
                                           self.current_high)


class QuantitativeFactor(NumericFactor):

    """ Real value factors. """

    type = 'quantitative'


class OrdinalFactor(NumericFactor):

    """ Ordinal (integer) factors.

    Attributes are checked to be integers (or None/inf if allowed).
    """

    type = 'ordinal'

    def __setattr__(self, attribute, value):
        """ Check values `current_low`, `current_high`, `max` and `min`.

        :param str attribute: Attribute name
        :param Any value: New value
        """
        numeric_attributes = ('current_low', 'current_high',
                              'max', 'min')
        if attribute in numeric_attributes:
            err_msg = '{} requires an integer, not {}'.format(attribute, value)
            if attribute == 'max' and value == float('inf'):
                pass
            elif attribute == 'min' and value == float('-inf'):
                pass
            elif isinstance(value, float) and not value.is_integer():
                raise ValueError(err_msg)
            elif isinstance(value, (float, int)):
                value = int(value)
            elif attribute in ('current_low', 'current_high') and value is None:
                pass
            else:
                raise ValueError(err_msg)

        super(OrdinalFactor, self).__setattr__(attribute, value)


class CategoricalFactor:

    """ Multilevel categorical factors. """

    type = 'categorical'

    def __init__(self, *args, **kwargs):
        raise NotImplementedError


class ExperimentDesigner:

    _matrix_designers = {
        'fullfactorial2levels': pyDOE2.ff2n,
        'fullfactorial3levels': lambda n: pyDOE2.fullfact([3] * n),
        'placketburman': pyDOE2.pbdesign,
        'boxbehnken': lambda n: pyDOE2.bbdesign(n, 1),
        'ccc': lambda n: pyDOE2.ccdesign(n, (0, 1), face='ccc'),
        'ccf': lambda n: pyDOE2.ccdesign(n, (0, 1), face='ccf'),
        'cci': lambda n: pyDOE2.ccdesign(n, (0, 1), face='cci'),
    }

    def __init__(self, factors, design_type, responses, skip_screening=True,
                 at_edges='distort', relative_step=.25, gsd_reduction='auto'):
        try:
            assert at_edges in ('distort', 'shrink'),\
                'unknown action at_edges: {0}'.format(at_edges)
            assert relative_step is None or 0 < relative_step < 1,\
                'relative_step must be float between 0 and 1 not {}'.format(relative_step)
        except AssertionError as e:
            raise ValueError(str(e))

        self.factors = OrderedDict()
        for factor_name, f_spec in factors.items():
            has_neg = any([f_spec['high_init'] < 0, f_spec['low_init'] < 0])
            f_min = f_spec.get('min', float('-inf') if has_neg else 0)
            f_max = f_spec.get('max', float('inf'))
            f_type = f_spec.get('type', 'quantitative')
            factor = Factor(f_type, f_max, f_min)
            factor.current_high = f_spec['high_init']
            factor.current_low = f_spec['low_init']
            self.factors[factor_name] = factor
            logging.debug('Sets factor {}: {}'.format(factor_name, factor))

        self.skip_screening = skip_screening
        self.step_length = relative_step
        self.design_type = design_type
        self.responses = responses
        self.gsd_reduction = gsd_reduction
        self._edge_action = at_edges
        self._phase = 'optimization' if self.skip_screening else 'screening'
        n = len(self.factors)
        try:
            self._matrix_designers[self.design_type.lower()]
        except KeyError:
            raise UnsupportedDesign(self.design_type)

        if len(self.responses) > 1:
            self._desirabilites = {name: make_desirability_function(factor)
                                   for name, factor in self.responses.items()}
        else:
            self._desirabilites = None

    def new_design(self):
        """

        :return: Experimental design-sheet.
        :rtype: pandas.DataFrame
        """
        if self._phase == 'screening':
            return self._new_screening_design(reduction=self.gsd_reduction)
        else:
            return self._new_optimization_design()

    def update_factors_from_response(self, response, degree=2, tol=.25):
        """ Calculate optimal factor settings given response and update
        factor settings to center around optimum. Returns calculated
        optimum.

        If several responses are defined, the geometric mean of Derringer
        and Suich's desirability functions will be used for optimization,
        see:

        Derringer, G., and Suich, R., (1980), "Simultaneous Optimization
        of Several Response Variables," Journal of Quality Technology, 12,
        4, 214-219.

        :param pandas.DataFrame response: Response sheet.
        :param int degree: Degree of polynomial to fit.
        :param float tol: Accepted relative distance to design space edge.
        :returns: Calculated optimum.
        :rtype: OptimizationResult
        """
        if response.shape[1] == 1:
            criterion = list(self.responses.values())[0]['criterion']
        else:
            raise NotImplementedError
        if self._phase == 'screening':
            return self._evaluate_screening(response, criterion)
        else:
            return self._evaluate_optimization(response, degree, tol, criterion)

    def _evaluate_optimization(self, response, degree, tol, criterion):
        # Find predicted optimal factor setting.
        logging.info('Finds optimum of current design.')
        optimal_x = predict_optimum(self._design_sheet, response,
                                    criterion, self.factors, degree)

        # Update factors around predicted optimal settings, but keep
        # the same span as previously.
        factor_info = [(f.span, f.current_high, f.current_low, f.center, f.type)
                       for f in self.factors.values()]
        spans, old_highs, old_lows, centers, types = map(np.array, zip(*factor_info))
        ratios = (old_highs - optimal_x) / spans

        if np.logical_and(ratios > tol, ratios < 1 - tol).all():
            converged = True
            logging.info('Convergence reached.')
        else:
            converged = False
            logging.info('Convergence not reached. Moves design.')

            # Calculate the new design center.
            if self.step_length is not None:
                allowed = spans * self.step_length
                diff = optimal_x - centers
                shift = allowed * (diff / np.linalg.norm(diff))
                new_center = centers + shift
            else:
                new_center = optimal_x

            logging.info('New design center {} (old {})'.format(new_center,
                                                                 centers))

            # Calculate the new highs and lows. Adjust the odrinal factors' values here.
            # FIXME: Should really new_center and spans be rounded before added?
            new_highs = [
                int(round(new_center[i]) + round(spans[i] / 2.0))
                if factor_type == 'ordinal' else new_center[i] + spans / 2.0
                for i, factor_type in enumerate(types)
            ]
            new_lows = [
                int(round(new_center[i]) - round(spans[i] / 2.0))
                if factor_type == 'ordinal' else new_center[i] - spans / 2.0
                for i, factor_type in enumerate(types)
            ]
            logging.info('Updates factor settings.')
            for i, key in enumerate(self._design_sheet.columns):
                self.factors[key].current_high = new_highs[i]
                self.factors[key].current_low = new_lows[i]
                logging.debug('New factor setting, {}: {}'.format(key,
                                                                  self.factors[key]))

        results = OptimizationResult(
            pd.Series(optimal_x, self._design_sheet.columns),
            converged, tol
        )
        logging.info('Predicted optimum: {}'.format(
            results.predicted_optimum))
        return results

    def _evaluate_screening(self, response, criterion):
        logging.info('Evaluates screening results.')
        if response.shape[1] > 1:
            raise NotImplementedError
        else:
            response = response.iloc[:, 0]
        factor_items = sorted(self.factors.items())
        if criterion == 'maximize':
            optimum_i = int(response.argmax())
        elif criterion == 'minimize':
            optimum_i = int(response.argmin())
        else:
            raise NotImplementedError

        optimum_design_row = self._design_matrix[optimum_i]
        optimum_settings = OrderedDict()

        # Update all factors according to current results. For each factor,
        # the current_high and current_low will be set to factors level above
        # and below the point in the screening design with the best response.
        for factor_level, (name, factor) in zip(optimum_design_row, factor_items):
            factor_levels = sorted(self._design_sheet[name].unique())
            optimum_settings[name] = factor_levels[factor_level]

            min_ = factor_levels[max([0, factor_level - 1])]
            max_ = factor_levels[min([factor_level + 1, len(factor_levels)])]

            if isinstance(factor, OrdinalFactor):
                min_ = int(np.round(min_))
                max_ = int(np.round(max_))

            factor.current_low = min_
            factor.current_high = max_
            logging.debug('New factor setting, {}: {}'.format(name, factor))

        results = OptimizationResult(
            pd.Series(optimum_settings), converged=False, tol=0
        )
        logging.info('Best screening result: {}'.format(
            results.predicted_optimum))

        self._phase = 'optimization'
        return results

    def _new_screening_design(self, reduction='auto'):
        factor_items = sorted(self.factors.items())

        levels = list()
        names = list()
        for name, factor in factor_items:
            names.append(name)

            if isinstance(factor, CategoricalFactor):
                levels.append(factor.values)
                continue

            num_levels = getattr(factor, 'screening_levels', 5)
            spacing = getattr(factor, 'screening_spacing', 'linear')
            min_ = factor.min
            max_ = factor.max
            if not np.isfinite([min_, max_]).all():
                raise ValueError('Can\'t perform screening with unbounded factors')

            space = np.linspace if spacing == 'linear' else np.logspace
            values = space(min_, max_, num_levels)

            if isinstance(factor, OrdinalFactor):
                values = sorted(np.unique(np.round(values)))

            levels.append(values)

        design_matrix = pyDOE2.gsd([len(values) for values in levels],
                                   reduction if reduction is not 'auto' else len(levels))
        factor_matrix = np.zeros_like(design_matrix)
        for i, values in enumerate(levels):
            factor_matrix[:, i] = np.array(values)[design_matrix[:, i]]

        self._design_matrix = design_matrix
        self._design_sheet = pd.DataFrame(factor_matrix, columns=names)
        return self._design_sheet

    def _new_optimization_design(self):
        matrix_designer = self._matrix_designers[self.design_type.lower()]
        self._design_matrix = matrix_designer(len(self.factors))

        mins = np.array([f.min for f in self.factors.values()])
        maxes = np.array([f.max for f in self.factors.values()])
        span = np.array([f.span for f in self.factors.values()])
        centers = np.array([f.center for f in self.factors.values()])
        factor_matrix = self._design_matrix * (span / 2.0) + centers

        # Check if current settings are outside allowed design space.
        # Also, for factors that are specified as ordinal, adjust their values
        # in the design matrix to be rounded floats
        for i, (factor_name, factor) in enumerate(self.factors.items()):
            if factor.type == 'ordinal':
                factor_matrix[:,i] = np.round(factor_matrix[:,i])
            logging.debug('Current setting {}: {}'.format(factor_name, factor))

        if (factor_matrix < mins).any() or (factor_matrix > maxes).any():
            logging.warning(('Out of design space factors. Adjusts factors'
                             'by {}.'.format(self._edge_action + 'ing')))
            if self._edge_action == 'distort':

                # Simply cap out-of-boundary values at mins and maxes.
                capped_mins = np.maximum(factor_matrix, mins)
                capped_mins_and_maxes = np.minimum(capped_mins, maxes)
                factor_matrix = capped_mins_and_maxes

            elif self._edge_action == 'shrink':
                raise NotImplementedError

        self._design_sheet = pd.DataFrame(factor_matrix, columns=self.factors.keys())
        return self._design_sheet


def Factor(factor_type, *args, **kwargs):
    """ Factory function stratified by the factor_type parameter

    :param str factor_type: The factor type (ordinal, quantitative, categorical).
    :returns: Function corresponding to `factor_type`.
    :rtype: QuantitativeFactor, OrdinalFactor, CategoricalFactor.
    :raises: UnsupportedFactorType
    """
    if factor_type.lower() == "quantitative":
        return QuantitativeFactor(*args, **kwargs)
    elif factor_type.lower() == "ordinal":
        return OrdinalFactor(*args, **kwargs)
    elif factor_type.lower() == "categorical":
        return CategoricalFactor(*args, **kwargs)
    else:
        raise UnsupportedFactorType(str(factor_type))


def make_desirability_function(response):
    """ Define a Derringer and Suich desirability function.

    :param dict response_dict: Response variable config dictionary.
    :return: desirability function.
    :rtype: Callable
    """
    s = response.get('priority', 1)
    if response['criterion'] == 'target':
        L = response['low_limit']
        U = response['high_limit']
        T = response.get('target', (U + L) / 2)

        def desirability(y):
            if y < L or U < y:
                return 0
            elif L <= y <= T:
                return ((y - L) / (T - L)) ** s
            elif T <= y <= U:
                return ((y - U) / (T - U)) ** s

    elif response['criterion'] == 'maximize':
        L = response['low_limit']
        T = response['target']

        def desirability(y):
            if y < L:
                return 0
            elif T < y:
                return 1
            else:
                return ((y - L) / (T - L)) ** s

    elif response['criterion'] == 'minimize':
        U = response['high_limit']
        T = response['target']

        def desirability(y):
            if y < T:
                return 1
            elif U < y:
                return 0
            else:
                return ((y - U) / (T - U)) ** s

    else:
        raise ValueError(response['criterion'])

    return desirability


def predict_optimum(design_sheet, response, criterion, factors, degree=2):
    """ Regress using `degree`-polynomial and optimize response.

    :param pandas.DataFrame design_sheet: Factor settings.
    :param pandas.Series response: Response Y-vector.
    :param str criterion: 'maximize' | 'minimize'
    :param int degree: Degree of polynomial to use for regression.
    :return: Array with predicted optimum.
    :rtype: numpy.ndarray[float]
    :raises: OptimizationFailed
    """
    matrix_columns = design_sheet.columns
    n_rows, n_cols = design_sheet.shape
    products = OrderedDict()
    combinations = [list(comb) for deg in range(1, degree + 1)
                    for comb in combinations_with_replacement(range(n_cols), deg)]

    # Multiply columns together.
    for column_group in (matrix_columns[comb] for comb in combinations):
        values = design_sheet[column_group].product(axis=1)
        products['*'.join(column_group)] = values

    extended_sheet = pd.DataFrame(products)

    # Extend data-sheet with constants-column.
    design_w_constants = np.hstack([np.ones((n_rows, 1)),
                                    extended_sheet.values])

    # Regress using least squares.
    c_stacked = np.linalg.lstsq(design_w_constants, response.values)[0]
    coefficients = c_stacked.flatten()
    # Define optimization function for optimizer.
    def predicted_response(x, invert=False):
        # Make extended factor list from given X.
        factor_list = [1] + [np.product(x[comb]) for comb in combinations]
        factors = np.array(factor_list)

        # Calculate response.
        factor_contributions = np.multiply(factors, coefficients)
        return (-1 if invert else 1) * factor_contributions.sum()

    # Since factors are set according to design the optimum is already
    # guessed to be at the center of the design. Hence, use medians as
    # initial guess.
    x0 = design_sheet.median(axis=0).values

    # Set up bounds for optimization to keep it inside the allowed design space.
    mins = [f.min if f.min != '-inf' else None for f in factors.values()]
    maxes = [f.max if f.max != 'inf' else None for f in factors.values()]
    bounds = list(zip(mins, maxes))

    if criterion == 'maximize':
        optimization_results = minimize(lambda x: predicted_response(x, True),
                                        x0, method='L-BFGS-B', bounds=bounds)
    elif criterion == 'minimize':
        optimization_results = minimize(predicted_response, x0,
                                        method='L-BFGS-B', bounds=bounds)

    if not optimization_results['success']:
        raise OptimizationFailed(optimization_results['message'])

    return optimization_results['x']
