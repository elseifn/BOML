from __future__ import absolute_import, print_function, division

from collections import OrderedDict, deque

# import py_bml.OuterOpt.outer_opt_utils as utils
import numpy as np
import tensorflow as tf
from tensorflow.python.training import slot_creator

import boml.extension
from boml import utils
from boml.ul_problem.BOMLOuterGrad import BOMLOuterGrad

RAISE_ERROR_ON_DETACHED = False


class BOMLOuterGradReverse(BOMLOuterGrad):

    def __init__(self, inner_method='Reverse', truncate_iter=-1, name='BMLOuterGradReverse'):
        """
       Utility method to initialize truncated reverse HG (not necessarily online),
       :param truncate_iter: Maximum number of iterations that will be stored
       :param name: a name for the operations and variables that will be created
       :return: ReverseHG object
           """
        super(BOMLOuterGradReverse, self).__init__(name)
        self._inner_method = inner_method
        self._alpha_iter = tf.no_op()
        self._reverse_initializer = tf.no_op()
        self._diff_initializer = tf.no_op()
        self._darts_initializer = tf.no_op()
        self._history = deque(maxlen=truncate_iter + 1) if truncate_iter >= 0 else []

    # noinspection SpellCheckingInspection
    def compute_gradients(self, outer_objective, optimizer_dict, meta_param=None, param_dict=OrderedDict()):
        """
        Function that adds to the computational graph all the operations needend for computing
        the hypergradients in a "dynamic" way, without unrolling the entire optimization graph.
        The resulting computation, while being roughly 2x more expensive then unrolling the
        optimizaiton dynamics, requires much less (GPU) memory and is more flexible, allowing
        to set a termination condition to the parameters optimizaiton routine.

        :param optimizer_dict: OptimzerDict object resulting from the inner objective optimization.
        :param outer_objective: A loss function for the outer parameters (scalar tensor)
        :param meta_param: Optional list of outer parameters to consider. If not provided will get all variables in the
                            hyperparameter collection in the current scope.

        :return: list of outer parameters involved in the computation
        """
        meta_param = super(BOMLOuterGradReverse, self).compute_gradients(outer_objective, optimizer_dict, meta_param)

        with tf.variable_scope(outer_objective.op.name):
            doo_ds = tf.gradients(outer_objective, list(optimizer_dict.state))
            alphas = self._create_lagrangian_multipliers(optimizer_dict, doo_ds)

            alpha_vec = utils.vectorize_all(alphas)
            dyn_vec = utils.vectorize_all(list(optimizer_dict.dynamics))
            lag_phi_t = utils.dot(alpha_vec, dyn_vec, name='iter_wise_lagrangian_part1')
            # TODO outer_objective might be a list... handle this case

            alpha_dot_B = tf.gradients(lag_phi_t, meta_param)
            if optimizer_dict.init_dynamics is not None:
                lag_phi0 = utils.dot(alpha_vec, utils.vectorize_all([d for (s, d) in optimizer_dict.init_dynamics]))
                alpha_dot_B0 = tf.gradients(lag_phi0, meta_param)
            else:
                alpha_dot_B0 = [None] * len(meta_param)

            # here, if some of this is None it may mean that the hyperparameter compares inside phi_0: check that and
            # if it is not the case raise error...
            hyper_grad_vars, hyper_grad_step = [], tf.no_op()
            for dl_dh, a_d_b0, hyper in zip(alpha_dot_B, alpha_dot_B0, meta_param):
                assert dl_dh is not None or a_d_b0 is not None, BOMLOuterGrad._ERROR_HYPER_DETACHED.format(hyper)
                hgv = None
                if dl_dh is not None:
                    hgv = self._create_outergradient(outer_objective, hyper)

                    hyper_grad_step = tf.group(hyper_grad_step, hgv.assign_add(dl_dh))
                if a_d_b0 is not None:
                    hgv = hgv + a_d_b0 if hgv is not None else a_d_b0
                    # here hyper_grad_step has nothing to do...
                hyper_grad_vars.append(hgv)
                # first update hypergradinet then alphas.
            with tf.control_dependencies([hyper_grad_step]):
                _alpha_iter = tf.group(*[alpha.assign(dl_ds) for alpha, dl_ds
                                         in zip(alphas, tf.gradients(lag_phi_t, list(optimizer_dict.state)))])
            self._alpha_iter = tf.group(self._alpha_iter, _alpha_iter)
            # put all the backward iterations toghether
            [self._hypergrad_dictionary[h].append(hg) for h, hg in zip(meta_param, hyper_grad_vars)]
            self._reverse_initializer = tf.group(self._reverse_initializer,
                                                 tf.variables_initializer(alphas),
                                                 tf.variables_initializer([h for h in hyper_grad_vars
                                                                           if hasattr(h, 'initializer')]))
            return meta_param

    @staticmethod
    def _create_lagrangian_multipliers(optimizer_dict, doo_ds):
        lag_mul = [slot_creator.create_slot(v.initialized_value(), utils.val_or_zero(der, v), 'alpha') for v, der
                   in zip(optimizer_dict.state, doo_ds)]
        [tf.add_to_collection(boml.extension.GraphKeys.LAGRANGIAN_MULTIPLIERS, lm) for lm in lag_mul]
        boml.extension.remove_from_collection(boml.extension.GraphKeys.GLOBAL_VARIABLES, *lag_mul)
        # this prevents the 'automatic' initialization with tf.global_variables_initializer.
        return lag_mul

    @staticmethod
    def _create_outergradient_from_dodh(hyper, doo_dhypers):
        """
        Creates one hyper-gradient as a variable. doo_dhypers:  initialization, that is the derivative of
        the outer objective w.r.t this hyper
        """
        hgs = slot_creator.create_slot(hyper, utils.val_or_zero(doo_dhypers, hyper), 'outergradient')
        boml.extension.remove_from_collection(boml.extension.GraphKeys.GLOBAL_VARIABLES, hgs)
        return hgs

    @staticmethod
    def _create_outergradient(outer_obj, hyper):
        return BOMLOuterGradReverse._create_outergradient_from_dodh(hyper, tf.gradients(outer_obj, hyper)[0])

    def _state_feed_dict_generator(self, history, T_or_generator):
        for t, his in zip(utils.solve_int_or_generator(T_or_generator), history):
            yield t, utils.merge_dicts(
                *[od.state_feed_dict(h) for od, h in zip(sorted(self._optimizer_dicts), his)]
            )

    def apply_gradients(self, inner_objective_feed_dicts=None, outer_objective_feed_dicts=None,
                        initializer_feed_dict=None, param_dict=OrderedDict(), train_batches=None, experiments=[], global_step=None, session=None,
                        online=False, callback=None):
        # callback may be a pair, first for froward pass, second for reverse pass
        if self._inner_method == 'Aggr':
            alpha = param_dict['alpha']
            t_tensor = param_dict['t_tensor']
        callback = utils.as_tuple_or_list(callback)
        # same thing for T
        T_or_generator = utils.as_tuple_or_list(param_dict['T'])

        ss = session or tf.get_default_session()

        self._history.clear()

        def _adjust_step(_t):
            if online:
                _T = utils.maybe_eval(global_step, ss)
                if _T is None:
                    _T = 0
                tot_t = T_or_generator[0]
                if not isinstance(tot_t, int): return _t  # when using a generator there is little to do...
                return int(_t + tot_t * _T)
            else:
                return _t

        if not online:
            _fd = utils.maybe_call(initializer_feed_dict, utils.maybe_eval(global_step, ss))
            self._save_history(ss.run(self.initialization, feed_dict=_fd))

        # else:  # not totally clear if i should add this
        #     self._save_history(ss.run(list(self.state)))

        T = 0  # this is useful if T_or_generator is indeed a generator...
        for t in utils.solve_int_or_generator(T_or_generator[0]):
            # nonlocal t  # with nonlocal would not be necessary the variable T... not compatible with 2.7

            _fd = utils.maybe_call(inner_objective_feed_dicts, _adjust_step(t))
            if self._inner_method == 'Aggr':
                _fd.update(utils.maybe_call(outer_objective_feed_dicts, _adjust_step(t)))
                if alpha.get_shape().as_list() == []:
                    _fd[t_tensor] = float(t + 1.0)
                else:
                    tmp = np.zeros((alpha.get_shape().as_list()[1], 1))
                    tmp[t][0] = 1.0
                    _fd[t_tensor] = tmp

            self._save_history(ss.run(self.iteration, feed_dict=_fd))
            T = t

            utils.maybe_call(callback[0], _adjust_step(t), _fd, ss)  # callback

        # initialization of support variables (supports stochastic evaluation of outer objective via global_step ->
        # variable)
        reverse_init_fd = utils.maybe_call(outer_objective_feed_dicts, utils.maybe_eval(global_step, ss))
        # now adding also the initializer_feed_dict because of tf quirk...
        maybe_init_fd = utils.maybe_call(initializer_feed_dict, utils.maybe_eval(global_step, ss))
        reverse_init_fd = utils.merge_dicts(reverse_init_fd, maybe_init_fd)
        ss.run(self._reverse_initializer, feed_dict=reverse_init_fd)

        del self._history[-1]  # do not consider last point

        for pt, state_feed_dict in self._state_feed_dict_generator(reversed(self._history), T_or_generator[-1]):
            # this should be fine also for truncated reverse... but check again the index t
            t = T - pt - 1  # if T is int then len(self.history) is T + 1 and this numerator
            # shall start at T-1

            new_fd = utils.merge_dicts(state_feed_dict, utils.maybe_call(inner_objective_feed_dicts,
                                                                         _adjust_step(t)))

            if self._inner_method == 'Aggr':
                new_fd = utils.merge_dicts(new_fd, utils.maybe_call(outer_objective_feed_dicts,
                                                                    _adjust_step(t)))
                # modified - mark
                if not alpha.shape.as_list():
                    new_fd[t_tensor] = float(t + 2.0)
                else:
                    tmp = np.zeros((alpha.get_shape().as_list()[1], 1))
                    tmp[t][0] = 1
                    new_fd[t_tensor] = tmp
            ss.run(self._alpha_iter, new_fd)
            if len(callback) == 2:
                utils.maybe_call(callback[1], _adjust_step(t), new_fd, ss)

    def _save_history(self, weights):
        self._history.append(weights)

    def hypergrad_callback(self, hyperparameter=None, flatten=True):
        """callback that records the partial hypergradients on the reverse pass"""
        values = []
        gs = list(self._hypergrad_dictionary.values()) if hyperparameter is None else \
            self._hypergrad_dictionary[hyperparameter]
        if flatten:
            gs = utils.vectorize_all(gs)

        # noinspection PyUnusedLocal
        def _callback(_, __, ss):
            values.append(ss.run(gs))  # these should not depend from any feed dictionary

        return values, _callback