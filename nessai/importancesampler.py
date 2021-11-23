# -*- coding: utf-8 -*-
"""
Importance nested sampler.
"""
import datetime
import logging
import os
from typing import Any, Literal, Optional

import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import entropy
from scipy.special import logsumexp

from .evidence import _INSIntegralState
from .basesampler import BaseNestedSampler
from .model import Model
from .posterior import draw_posterior_samples
from .proposal.importance import ImportanceFlowProposal
from .plot import plot_1d_comparison
from .livepoint import (
    add_extra_parameters_to_live_points,
    get_dtype,
    live_points_to_dict,
    numpy_array_to_live_points,
)
from .utils.information import cumulative_entropy
from .utils.stats import effective_sample_size, weighted_quantile


logger = logging.getLogger(__name__)


def _initialize_global_variables(model):
    """
    Store a global copy of the model for multiprocessing.
    """
    global _model
    _model = model


def _log_likelihood_wrapper(x):
    """
    Wrapper for the log likelihood
    """
    return _model.evaluate_log_likelihood(x)


class ImportanceNestedSampler(BaseNestedSampler):
    """

    Parameters
    ----------
    model
        User-defined model.
    nlive
        Number of live points.
    proposal_config
        Configuration for the proposal object. This includes the normalising
        flow.
    tolerance
        Tolerance for determining when to stop the sampler.
    stopping_condition
        Choice of stopping condition to use.
    level_method
        Method for determining new levels.
    """
    def __init__(
        self,
        model: Model,
        nlive: int,
        proposal_config: Optional[dict] = None,
        min_iteration: Optional[int] = None,
        max_iteration: Optional[int] = None,
        min_samples: int = 1000,
        tolerance: float = 0.1,
        n_update: Optional[int] = None,
        use_n_effective: bool = False,
        plot_pool: bool = True,
        replace_all: bool = False,
        update_nested_samples: bool = False,
        level_method: Literal['entropy', 'quantile'] = 'entropy',
        leaky: bool = True,
        n_pool: Optional[int] = None,
        stopping_condition: Literal['evidence', 'kl'] = 'evidence',
        min_dZ: Optional[float] = 5.0,
        level_kwargs=None,
        **kwargs: Any
    ):

        add_extra_parameters_to_live_points(['logW', 'logG'])

        super().__init__(model, nlive, **kwargs)
        self.state = _INSIntegralState()

        self._posterior_samples = None
        self.initialised = False
        self.finalised = False
        self.history = None
        self.pool = None
        self.dZ = np.inf

        self.tolerance = tolerance
        self.stopping_condition = stopping_condition
        self.min_samples = min_samples
        self.condition = np.inf
        self.stop = False
        self.n_update = n_update
        self.plot_pool = plot_pool
        self.use_n_effective = use_n_effective
        self.replace_all = replace_all
        self._update_nested_samples = update_nested_samples
        self.leaky = leaky
        self.n_pool = n_pool
        self.level_method = level_method
        self.level_kwargs = {} if level_kwargs is None else level_kwargs
        self.current_entropy = 0.0
        self.current_live_points_entropy = 0.0
        self.current_ns_entropy = 0.0
        self.smc_dZ = np.inf
        self.current_log_evidence = -np.inf

        self.min_dZ = min_dZ if min_dZ is not None else np.inf

        self.proposal = self.get_proposal(proposal_config)
        self.proposal_config = proposal_config
        self.configure_iterations(min_iteration, max_iteration)

        self.nested_samples = np.empty(0, dtype=get_dtype(self.model.names))

        self.update_level_time = datetime.timedelta()

        if self.replace_all:
            self._update_nested_samples = False

    @property
    def log_evidence(self) -> float:
        return self.state.logZ

    @property
    def log_evidence_error(self) -> float:
        return self.state.compute_uncertainty()

    @property
    def live_points_entropy(self):
        log_p = self.live_points['logL'] + self.live_points['logW']
        log_p -= logsumexp(log_p)
        p = np.exp(log_p)
        return entropy(p)

    @property
    def nested_samples_entropy(self):
        log_p = self.nested_samples['logL'] + self.nested_samples['logW']
        log_p -= logsumexp(log_p)
        p = np.exp(log_p)
        return entropy(p) / np.log(p.size)

    def get_proposal(
        self,
        config: Optional[dict] = None,
        subdir: str = 'levels'
    ):
        """Configure the proposal."""
        output = os.path.join(self.output, subdir, '')
        if config is None:
            config = {}
        proposal = ImportanceFlowProposal(
            self.model,
            output,
            self.nlive,
            combined_proposal=not self.replace_all,
            **config
        )
        proposal.set_log_likelihood(self.log_likelihood)
        return proposal

    def configure_iterations(
        self,
        min_iteration: Optional[int],
        max_iteration: Optional[int]
    ) -> None:
        """Configure the maximum iteration."""
        if min_iteration is None:
            self.min_iteration = -1
        else:
            self.min_iteration = int(min_iteration)
        if max_iteration is None:
            self.max_iteration = np.inf
        else:
            self.max_iteration = int(max_iteration)

    def configure_pool(self) -> None:
        """
        Configure the multiprocessing pool
        """
        if self.pool is None and self.n_pool is not None:
            logger.info(
                f'Starting multiprocessing pool with {self.n_pool} processes')
            import multiprocessing
            self.pool = multiprocessing.Pool(
                processes=self.n_pool,
                initializer=_initialize_global_variables,
                initargs=(self.model,)
            )
        elif self.pool is not None:
            logger.debug('Pool already initialised')
        else:
            logger.info('n_pool is none, no multiprocessing pool')

    def close_pool(self, code: Optional[int] = None):
        """
        Close the the multiprocessing pool
        """
        if getattr(self, "pool", None) is not None:
            logger.info("Starting to close worker pool.")
            if code == 2:
                self.pool.terminate()
            else:
                self.pool.close()
            self.pool.join()
            self.pool = None
            logger.info("Finished closing worker pool.")

    def log_likelihood(self, samples: np.ndarray) -> None:
        """Update the log-likelihood in place."""
        logger.debug('Evaluating likelihoods')
        if self.pool is not None:
            samples['logL'] = self.pool.map(
                _log_likelihood_wrapper,
                samples,
            )
            self.model.likelihood_evaluations += samples.size
        elif self.model.has_vectorised_likelihood:
            samples['logL'] = \
                self.model.evaluate_log_likelihood(samples)
        else:
            samples['logL'] = \
                list(map(self.model.evaluate_log_likelihood, samples))

    def populate_live_points(self) -> None:
        """Draw the initial live points from the prior.

        The live points are automatically sorted and asigned the iteration
        number -1.
        """
        live_points = self.model.from_unit_hypercube(
            numpy_array_to_live_points(
                np.random.rand(self.nlive, self.model.dims),
                self.model.names
            )
        )
        self.log_likelihood(live_points)
        live_points['it'] = -np.ones(live_points.size)
        # Since log_g is computed in the unit-cube
        live_points['logP'] = self.model.log_prior(live_points)
        live_points['logG'] = np.log(self.nlive)
        live_points['logW'] = - live_points['logG']
        self.live_points = np.sort(live_points, order='logL')

    def initialise(self) -> None:
        """Initialise the nested sampler.

        Draws live points, initialises the proposal.
        """
        if self.initialised:
            logger.warning('Nested sampler has already initialised!')
        if self.live_points is None:
            self.populate_live_points()

        self.configure_pool()

        self.initialise_history()
        self.proposal.initialise()
        self.initialised = True

    def initialise_history(self) -> None:
        """Initialise the dictionary to store history"""
        if self.history is None:
            logger.debug('Initialising history dictionary')
            self.history = dict(
                min_logL=[],
                max_logL=[],
                logZ=[],
                dZ=[],
                alt_dZ=[],
                n_added=[],
                n_removed=[],
                n_post=[],
                live_points_entropy=[],
                live_points_remaining_entropy=[],
                pool_entropy=[],
                likelihood_evaluations=[],
                max_log_g=[],
                mean_log_g=[],
                median_log_g=[],
                min_log_g=[],
                kl=[],
                kl_g_live_points=[],
                kl_g_nested_samples=[],
                kl_proposals=[],
            )
        else:
            logger.debug('History dictionary already initialised')

    def update_history(self) -> None:
        """Update the history dictionary"""
        self.history['min_logL'].append(np.min(self.live_points['logL']))
        self.history['max_logL'].append(np.max(self.live_points['logL']))
        self.history['logZ'].append(self.state.logZ)
        self.history['dZ'].append(self.dZ)
        self.history['alt_dZ'].append(self.alt_dZ)
        self.history['n_post'].append(self.state.effective_n_posterior_samples)
        self.history['live_points_entropy'].append(
            entropy(np.exp(self.live_points['logW']))
        )
        self.history['live_points_remaining_entropy'].append(
            self.entropy_remaining
        )
        self.history['pool_entropy'] = self.proposal._history['entropy']
        self.history['likelihood_evaluations'].append(
            self.model.likelihood_evaluations
        )
        self.history['max_log_g'].append(np.max(self.live_points['logG']))
        self.history['mean_log_g'].append(np.mean(self.live_points['logG']))
        self.history['median_log_g'].append(
            np.median(self.live_points['logG'])
        )
        self.history['min_log_g'].append(np.min(self.live_points['logG']))
        self.history['kl'].append(self.post_kl)

    def determine_level_quantile(self, q: Optional[float] = None) -> int:
        """Determine where the next level should be located.

        Computes the q'th quantile based on log-likelihood and log-weights.

        Parameters
        ----------
        q : float
            Quantile to use. Defaults to 1 - 1/e.

        Returns
        -------
        int
            The number of live points to discard.
        """
        if q is None:
            q = 1.0 - 1.0 / np.e
        logger.debug(f'Determining {q:.3f} quantile')
        a = self.live_points['logL']
        weights = np.exp(self.live_points['logW'], dtype=np.float64)
        cutoff = weighted_quantile(a, q, weights=weights, values_sorted=True)
        n = np.argmax(a >= cutoff)
        logger.debug(f'{q:.3} quantile is logL + logW ={cutoff:.3}')
        return int(n)

    def determine_level_entropy(self, bits: float = 0.5) -> int:
        log_p = self.live_points['logL'] + self.live_points['logW']
        log_p -= logsumexp(log_p)
        p = np.exp(log_p)
        h = cumulative_entropy(p[::-1])
        logger.debug(f'Entropy in live points: {h[-1]}')
        n = self.live_points.size - np.argmax(h > (h[-1] - bits))
        return int(n)

    def determine_level(self, method='entropy', **kwargs) -> int:
        """Determine where the next level should.

        Returns
        -------
        float :
            The log-likelihood of the quantile
        int :
            The number of samples to discard.
        """
        if method == 'quantile':
            n = self.determine_level_quantile(**kwargs)
        elif method == 'entropy':
            n = self.determine_level_entropy(**kwargs)
        else:
            raise ValueError(method)
        logger.info(f'Next level should remove {n} points')
        return n

    def update_level(self):
        """Update the current likelihood contour"""
        st = datetime.datetime.now()
        logger.debug('Updating the contour')
        logger.info(
            "Training data ESS: "
            f"{effective_sample_size(self.training_points['logW'])}"
        )
        self.proposal.train(
            self.training_points,
            plot=self.proposal.plot_training
        )
        kl = self.proposal.compute_kl_between_proposals(
            self.training_points, p_it=self.iteration - 1, q_it=self.iteration,
        )
        self.history['kl_proposals'].append(kl)
        self.update_level_time += (datetime.datetime.now() - st)

    def update_live_points(self, live_points: np.ndarray = None):
        if live_points is None:
            logger.debug('Updating existing live points')
            if self.live_points is None:
                logger.warning('No live points to update!')
                return
            else:
                live_points = self.live_points
        self.proposal.update_samples(live_points)

    def update_nested_samples(self) -> None:
        """Update the nested samples to reflect the current g."""
        logger.debug('Updating all nested samples')
        self.proposal.update_samples(self.nested_samples)

    def draw_n_samples(self, n: int):
        """Draw n points from the proposal"""
        if not self.leaky:
            logL_min = self.min_logL
        else:
            logL_min = None
        new_points = self.proposal.draw(n, logL_min=logL_min)
        if self.leaky:
            logger.info('Evaluating likelihood for new points')
            self.log_likelihood(new_points)
        return new_points

    def add_and_update_points(self, n: int):
        """Add new points to the current set of live points.

        Parameters
        ----------
        n : int
            The number of points to add.
        """
        logger.debug(f'Adding {n} points')
        new_points = self.draw_n_samples(n)
        new_points.sort(order='logL')
        new_points['it'] = self.iteration
        logger.info(
            "New samples ESS: "
            f"{effective_sample_size(new_points['logW'])}"
        )

        self.update_live_points()
        if self._update_nested_samples:
            self.update_nested_samples()
        self.history['n_added'].append(new_points.size)

        if self.plot and self.plot_pool:
            plot_1d_comparison(
                self.training_points,
                new_points,
                filename=os.path.join(
                    self.output, 'levels', f'pool_{self.iteration}.png'
                )
            )
        if self.leaky:
            logger.debug('Adding all points to the live points.')
            if self.live_points is None:
                self.live_points = new_points
            else:
                idx = np.searchsorted(
                    self.live_points['logL'], new_points['logL']
                )
                self.live_points = np.insert(self.live_points, idx, new_points)
        else:
            logger.debug(
                f'Only add points above logL={self.min_logL:3f} to the '
                'live points.'
            )
            cut = np.argmax(new_points['logL'] >= self.min_logL)
            self.add_to_nested_samples(new_points[:cut])
            idx = np.searchsorted(
                self.live_points['logL'], new_points[cut:]['logL']
            )
            self.live_points = np.insert(
                self.live_points, idx, new_points[cut:]
            )

    def add_to_nested_samples(self, samples: np.ndarray) -> None:
        """Add an array of samples to the nested samples."""
        self.nested_samples = np.concatenate([self.nested_samples, samples])

    def remove_points(self, n: int) -> None:
        """Remove points from the current set of live points.

        The evidence is updated with the discared points.

        Parameters
        ----------
        n : int
            The number of points to remove.
        """
        self.history['n_removed'].append(n)
        logger.debug(f'Removing {n} points')
        self.add_to_nested_samples(self.live_points[:n])
        if self._update_nested_samples:
            self.state.update_evidence_from_nested_samples(
                self.nested_samples
            )
        else:
            self.state.update_evidence(self.live_points[:n])
        if self.replace_all:
            self.training_points = self.live_points[n:].copy()
            self.live_points = None
        else:
            self.live_points = np.delete(self.live_points, np.s_[:n])
            self.training_points = self.live_points.copy()
        self.entropy_remaining = entropy(
            np.exp(self.training_points['logW'])
        )
        logger.warning(
            f'Update {self.iteration} - '
            f'log Z: {self.state.logZ:.3f} +/- '
            f'{self.state.compute_uncertainty():.3f} '
            f'dZ: {self.state.compute_condition(self.live_points):.3f} '
            f'H: {self.entropy_remaining:.3f} '
            f'ESS: {self.state.effective_n_posterior_samples:.1f} '
            # f"logL min: {self.live_points['logL'].min():.3f} "
            # f"logL max: {self.live_points['logL'].max():.3f}"
        )

    def finalise(self) -> None:
        """Finalise the sampling process."""
        if self.finalised:
            logger.warning('Sampler already finalised')
            return
        logger.info('Finalising')
        self.add_to_nested_samples(self.live_points)
        self.state.update_evidence_from_nested_samples(self.nested_samples)
        self.live_points = None
        final_kl = self.kl_divergence()
        logger.warning(
            f'Final log Z: {self.state.logZ:.3f} '
            f'+/- {self.state.compute_uncertainty():.3f}'
        )
        logger.warning(f'Final KL divergence: {final_kl:.3f}')
        logger.warning(
            f'Final ESS: {self.state.effective_n_posterior_samples:.3f}'
        )
        self.checkpoint(periodic=True)
        self.finalised = True

    def add_level_post_sampling(self, samples: np.ndarray, n: int) -> None:
        """Add a level to the nested sampler after initial sampling has \
            completed.
        """
        self.proposal.train(samples)
        new_samples = self.draw_n_samples(n)
        self.update_live_points(new_samples)
        if self._update_nested_samples:
            self.update_nested_samples(n)
        self.add_to_nested_samples(new_samples)
        self.state.update_evidence_from_nested_samples(self.nested_samples)

    def compute_stopping_condition(self) -> float:
        """Compute the stopping condition.

        The method used will depened on how the sampler was configured.
        """
        # Version for SMC_NS
        previous_log_evidence = self.current_log_evidence
        log_Z_with_live_points = self.state.compute_log_Z(self.live_points)

        self.smc_dZ = np.abs(
            log_Z_with_live_points - previous_log_evidence
        )

        current_ln_Z = self.state.logZ
        self.alt_dZ = np.abs(current_ln_Z - self.initial_ln_Z)
        logger.info(f'Alt dZ: {self.alt_dZ}')
        self.dZ = self.state.compute_condition(self.live_points)
        self.post_kl = self.kl_divergence(include_live_points=True)
        log_p = np.concatenate([
            self.live_points['logL'] + self.live_points['logW'],
            self.nested_samples['logL'] + self.nested_samples['logW']
        ])
        previous_entropy = self.current_entropy
        previous_live_points_entropy = self.current_live_points_entropy
        previous_ns_entropy = self.current_ns_entropy
        self.current_entropy = entropy(np.exp(log_p))
        self.current_live_points_entropy = self.live_points_entropy
        self.current_ns_entropy = entropy(
            np.exp(self.nested_samples['logL'] + self.nested_samples['logW'])
        )
        self.d_entropy = np.abs(
            (self.current_entropy - previous_entropy) / self.current_entropy
        )
        self.d_lp_entropy = np.abs(
            (self.current_live_points_entropy - previous_live_points_entropy)
            / self.current_live_points_entropy
        )
        self.d_ns_entropy = np.abs(
            (self.current_ns_entropy - previous_ns_entropy)
            / self.current_entropy
        )

        self.current_log_evidence = self.state.compute_log_Z(self.live_points)

        logger.info(f'Current entropy: {self.current_entropy:.3f}')
        logger.info(f'Relative change in entropy: {self.d_entropy:.3f}')
        logger.info(
            f'Current LP entropy: {self.current_live_points_entropy:.3f}'
        )
        logger.info(f'Relative change in LP entropy: {self.d_lp_entropy:.3f}')
        logger.info(f'Current NS entropy: {self.current_ns_entropy:.3f}')
        logger.info(f'Relative change in NS entropy: {self.d_ns_entropy:.3f}')
        if self.stopping_condition == 'evidence':
            cond = self.dZ
        elif self.stopping_condition == 'kl':
            cond = self.post_kl
        elif self.stopping_condition == 'alt_evidence':
            cond = self.alt_dZ
        elif self.stopping_condition == 'smc_evidence':
            cond = self.smc_dZ
        elif self.stopping_condition == 'entropy':
            cond = self.d_entropy
        elif self.stopping_condition == 'lp_entropy':
            cond = self.d_lp_entropy
        elif self.stopping_condition == 'ns_entropy':
            cond = self.d_ns_entropy
        else:
            raise ValueError(
                f'Unknown stopping criterion: {self.stopping_condition}'
            )
        logger.info(
            f'Stopping condition: {cond:.3f} - Tolerance: {self.tolerance:.3f}'
        )
        return cond

    def nested_sampling_loop(self):
        """Main nested sampling loop."""
        self.initialise()
        logger.warning('Starting the nested sampling loop')

        while True:
            if (
                self.condition <= self.tolerance
                and self.iteration >= self.min_iteration
            ):
                if self.dZ <= self.min_dZ:
                    logger.debug('Stopping')
                    break
            self.initial_ln_Z = self.state.logZ
            if self.n_update is None:
                n_remove = self.determine_level(
                    method=self.level_method, **self.level_kwargs
                )
            else:
                n_remove = self.n_update
            if n_remove == 0:
                logger.warning('No points to remove')
                break
            if (self.live_points.size - n_remove) < self.min_samples:
                n_remove = self.live_points.size - self.min_samples
                logger.critical('Cannot remove all live points!')
                logger.critical(f'Removing {n_remove}')

            self.min_logL = self.live_points[n_remove]['logL'].copy()
            self.remove_points(n_remove)
            self.update_level()
            if self.replace_all:
                n_add = self.nlive
            else:
                n_add = n_remove
            self.add_and_update_points(n_add)
            self.iteration += 1
            self.condition = self.compute_stopping_condition()
            logger.info(f'Live points entropy: {self.live_points_entropy}')
            logger.info(f'NS entropy: {self.nested_samples_entropy}')
            self.update_history()
            self.produce_plots()
            if self.iteration >= self.max_iteration:
                break

        logger.warning(
            f'Finished nested sampling loop after {self.iteration} iterations '
            f'with dZ = {self.dZ:.3f}'
        )
        self.finalise()
        self.close_pool()
        return self.log_evidence, self.nested_samples

    def draw_posterior_samples(
        self,
        sampling_method: str = 'rejection_sampling',
        n: Optional[int] = None
    ) -> np.ndarray:
        """Draw posterior samples from the current nested samples."""
        log_w = self.state.log_posterior_weights
        posterior_samples = draw_posterior_samples(
            self.nested_samples,
            log_w=log_w,
            method=sampling_method,
            n=n,
        )
        log_p = posterior_samples['logL'] + posterior_samples['logW']
        H = entropy(np.exp(log_p))
        logger.info(f'Information in the posterior: {H:.3f} nats')
        logger.info(f'Produced {posterior_samples.size} posterior samples.')
        return posterior_samples

    def kl_divergence(self, include_live_points: bool = False) -> float:
        """Compute the KL divergence between the posterior and g"""
        if not len(self.nested_samples):
            return np.inf
        # logG is computed on the unit hyper-cube where the prior is 1/1^n
        # so logP = 0
        log_q = self.nested_samples['logL'].copy()
        log_p = self.nested_samples['logG'].copy()
        if include_live_points:
            log_q = np.concatenate([log_q, self.live_points['logL']])
            log_p = np.concatenate([log_p, self.live_points['logG']])
        log_q -= logsumexp(log_q)
        log_p -= logsumexp(log_p)
        kl = np.mean(log_p - log_q)
        logger.info(f'KL divergence between posterior and g: {kl:.3f}')
        return float(kl)

    def draw_more_nested_samples(self, n: int) -> np.ndarray:
        """Draw more nested samples from g"""
        samples = self.proposal.draw_from_flows(n)
        samples['logL'] = self.model.evaluate_log_likelihood(samples)
        return samples

    def plot_state(
        self,
        filename: Optional[str] = None
    ) -> Optional[plt.figure]:
        """
        Produce plots with the current state of the nested sampling run.
        Plots are saved to the output directory specifed at initialisation.

        Parameters
        ----------
        filename
            If specifie the figure will be saved, otherwise the figure is
            returned.
        """
        fig, ax = plt.subplots(9, 1, sharex=True, figsize=(15, 12))
        ax = ax.ravel()
        its = np.arange(self.iteration)

        colours = ['#4575b4', '#d73027', '#fad117']
        ls = ['-', '--', ':', '-.']

        ax[0].plot(its, self.history['min_logL'], label='Min logL',
                   c=colours[0], ls=ls[0])
        ax[0].plot(its, self.history['max_logL'], label='Max logL',
                   c=colours[1], ls=ls[1])
        ax[0].set_ylabel('Log-likelihood')
        ax[0].legend(frameon=False)

        ax[1].plot(its, self.history['logZ'], label='logZ', c=colours[0],
                   ls=ls[0])
        ax[1].set_ylabel('Log-evidence')
        ax[1].legend(frameon=False)

        ax_dz = plt.twinx(ax[1])
        ax_dz.plot(its, self.history['dZ'], label='dZ', c=colours[1], ls=ls[1])
        ax_dz.set_ylabel('dZ')
        ax_dz.set_yscale('log')
        ax_dz.axhline(self.tolerance, label=f'dZ={self.tolerance}', ls=':',
                      c=colours[2])
        handles, labels = ax[1].get_legend_handles_labels()
        handles_dz, labels_dz = ax_dz.get_legend_handles_labels()
        ax[1].legend(handles + handles_dz, labels + labels_dz, frameon=False)

        ax[2].plot(its, self.history['live_points_entropy'],
                   label='Live points - combined', c=colours[0], ls=ls[0])
        ax[2].plot(its, self.history['pool_entropy'],
                   label='Pool', c=colours[1], ls=ls[0])
        ax[2].plot(its, self.history['live_points_remaining_entropy'],
                   label='Live points - remaining', c=colours[2], ls=ls[0])
        ax[2].axhline(np.log(self.nlive), label='Target', ls=ls[1],
                      c=colours[0])
        ax[2].set_ylabel('Entropy')
        ax[2].legend(frameon=False)

        ax[3].plot(its, self.history['kl_proposals'], label='(q_i||q_i-1)',
                   c=colours[0], ls=ls[0])
        ax[3].set_ylabel('KL divergence')
        ax_kl = plt.twinx(ax[3])
        ax_kl.plot(its, self.history['kl'], label='(g||post)', c=colours[1],
                   ls=ls[1])
        ax_kl.set_ylabel('KL divergence')

        ax[4].plot(its, self.history['n_removed'], ls=ls[0], c=colours[0],
                   label='Removed')
        ax[4].plot(its, self.history['n_added'], ls=ls[1], c=colours[1],
                   label='Added')
        ax[4].set_ylabel('# samples')
        ax[4].legend(frameon=False)

        ax[5].plot(its, self.history['max_log_g'], label='Max.')
        ax[5].plot(its, self.history['min_log_g'], label='Min.')
        ax[5].plot(its, self.history['mean_log_g'], label='Mean')
        ax[5].plot(its, self.history['median_log_g'], label='Median')
        ax[5].legend(frameon=False)
        ax[5].set_ylabel('Log g')

        ax[6].plot(its, self.history['likelihood_evaluations'])
        ax[6].set_ylabel('# likelihood evaluations')

        ax[7].plot(its, self.history['n_post'], label='# posterior samples')
        ax[7].set_ylabel('ESS')
        ax[7].legend(frameon=False)

        ax[8].plot(its, self.history['dZ'], label='dZ', c=colours[0], ls=ls[0])
        ax[8].plot(its, self.history['kl'], label='KL', c=colours[1], ls=ls[1])
        ax[8].plot(
            its, self.history['alt_dZ'], label='Alt. dZ', c=colours[2],
            ls=ls[2]
        )
        ax[8].legend(frameon=False)
        ax[8].set_ylabel('Stopping criteria')
        ax[8].set_yscale('log')

        ax[-1].set_xlabel('Iteration')

        fig.suptitle(f'Sampling time: {self.current_sampling_time}',
                     fontsize=16)

        fig.tight_layout()
        fig.subplots_adjust(top=0.95)
        if filename is not None:
            fig.savefig(filename)
            plt.close(fig)
        else:
            return fig

    def produce_plots(self, overide: bool = False) -> None:
        """Produce all of the relevant plots.

        Checks if plotting is enabled.

        Paramters
        ---------
        force : bool
            Overide the plotting setting and force the plots to be produced.
        """
        if self.plot or overide:
            logger.debug('Producing plots')
            self.plot_state(os.path.join(self.output, 'state.png'))
        else:
            logger.debug('Skipping plots')

    def get_result_dictionary(self):
        d = super().get_result_dictionary()
        d['history'] = self.history
        d['nested_samples'] = live_points_to_dict(self.nested_samples)
        d['likelihood_evaluations'] = self.model.likelihood_evaluations
        d['log_evidence'] = self.log_evidence
        d['log_evidence_error'] = self.log_evidence_error
        d['sampling_time'] = self.sampling_time.total_seconds()
        d['update_level_time'] = self.update_level_time.total_seconds()
        return d

    def __getstate__(self):
        obj = super().__getstate__()
        obj['pool'] = None
        return obj
