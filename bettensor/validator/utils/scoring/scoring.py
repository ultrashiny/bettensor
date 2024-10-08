"""
BetTensor Scoring Module. 

This module handles the scoring of miners based on their data. The scoring is intended to select for miners that deploy positive expected value strategies, with some degree of risk management. 
We mostly determine +EV through closing line value analysis.

Inputs: 
- Miner Predictions

Outputs: 
- A NumPy array of the composite scores for all miners, indexed by miner_uid. 
"""


import json
import numpy as np
import bittensor as bt
from datetime import datetime, timezone, timedelta
from typing import List, Dict

from bettensor.validator.utils.database.database_manager import DatabaseManager
from .scoring_data import ScoringData
from .entropy_system import EntropySystem


class ScoringSystem:
    def __init__(
        self,
        db_manager: DatabaseManager,
        num_miners: int = 256,
        max_days: int = 45,
        reference_date: datetime = datetime(
            year=2024, month=9, day=30, tzinfo=timezone.utc
        ),
    ):
        """
        Initialize the ScoringSystem.

        Args:
            db_path (str): Path to the database.
            num_miners (int): Number of miners.
            max_days (int): Maximum number of days to track.
            reference_date (datetime, optional): Reference date for scoring. Defaults to January 1, 2024.
        """
        self.num_miners = num_miners
        self.max_days = max_days
        self.num_tiers = (
            7  # 5 tiers + 2 for invalid UIDs (0) and empty network slots (-1)
        )
        self.valid_uids = set()  # Initialize as an empty set

        self.reference_date = reference_date
        self.invalid_uids = []
        self.epsilon = 1e-8  # Small constant to prevent division by zero
        self.db_manager = db_manager
        # Initialize tier configurations
        self.tier_configs = [
            {
                "window": 0,
                "min_wager": 0,
                "capacity": int(num_miners * 1),
                "incentive": 0,
            },  # Tier -1 for empty slots
            {
                "window": 0,
                "min_wager": 0,
                "capacity": int(num_miners * 1),
                "incentive": 0,
            },  # Tier 0 for invalid UIDs
            {
                "window": 3,
                "min_wager": 0,
                "capacity": int(num_miners * 1.0),
                "incentive": 0.1,
            },  # Tier 1
            {
                "window": 7,
                "min_wager": 4000,
                "capacity": int(num_miners * 0.2),
                "incentive": 0.15,
            },  # Tier 2
            {
                "window": 15,
                "min_wager": 10000,
                "capacity": int(num_miners * 0.2),
                "incentive": 0.2,
            },  # Tier 3
            {
                "window": 30,
                "min_wager": 20000,
                "capacity": int(num_miners * 0.1),
                "incentive": 0.25,
            },  # Tier 4
            {
                "window": 45,
                "min_wager": 35000,
                "capacity": int(num_miners * 0.05),
                "incentive": 0.3,
            },  # Tier 5
        ]

        # Initialize score arrays
        self.clv_scores = np.zeros((num_miners, max_days))
        self.roi_scores = np.zeros((num_miners, max_days))
        self.sortino_scores = np.zeros((num_miners, max_days))
        self.amount_wagered = np.zeros((num_miners, max_days))
        self.entropy_scores = np.zeros((num_miners, max_days))
        self.tiers = np.ones((num_miners, max_days), dtype=int)

        # Initialize composite scores. The last dimension is for a daily score [0], and the other 5 are a "tier score"
        # tier score is calculated as a rolling average over the scoring window of that tier. every miner gets a tier score for every tier

        self.composite_scores = np.zeros((num_miners, max_days, 6))

        # Scoring weights
        self.clv_weight = 0.30
        self.roi_weight = 0.30
        self.sortino_weight = 0.30
        self.entropy_weight = 0.10
        self.entropy_window = self.max_days

        self.scoring_data = ScoringData(db_manager, num_miners)
        self.entropy_system = EntropySystem(num_miners, max_days)
        self.incentives = []

        self.current_day = 0
        self.current_date = datetime.now(timezone.utc)  # Initialize current_date
        self.last_update_date = None

        # Try to load state from database
        self.init = self.load_state()

        # Handle potential downtime
        self.advance_day(self.current_date)

        self.tier_mapping = {
            0: "daily",  # Daily score
            1: "tier_1",
            2: "tier_2",
            3: "tier_3",
            4: "tier_4",
            5: "tier_5"
        }

    def advance_day(self, current_date):
        if self.last_update_date is None:
            self.last_update_date = current_date
            return

        days_passed = (current_date - self.last_update_date).days
        if days_passed > 0:
            old_day = self.current_day
            self.current_day = (self.current_day + days_passed) % self.max_days
            self.last_update_date = current_date
            self.current_date = current_date + timedelta(days=days_passed)
            bt.logging.info(
                f"Advanced {days_passed} day(s). New current_day index: {self.current_day}"
            )

            # Reset wager for the new current_day
            self.amount_wagered[:, self.current_day] = 0
            bt.logging.debug(f"Reset amount_wagered for day {self.current_day} to 0.0")

            # Carry over tier information
            self.tiers[:, self.current_day] = self.tiers[:, old_day]

            if days_passed > 1:
                self.handle_downtime(days_passed)

    def handle_downtime(self, days_passed):
        bt.logging.warning(
            f"System was down for {days_passed - 1} day(s). Adjusting scores..."
        )

        # For each day of downtime, we'll copy the last known scores and tiers
        for i in range(1, days_passed):
            prev_day = (self.current_day - days_passed + i) % self.max_days
            current_day = (self.current_day - days_passed + i + 1) % self.max_days

            self.clv_scores[:, current_day] = self.clv_scores[:, prev_day]
            self.roi_scores[:, current_day] = self.roi_scores[:, prev_day]
            self.entropy_scores[:, current_day] = self.entropy_scores[:, prev_day]
            self.composite_scores[:, current_day] = self.composite_scores[:, prev_day]
            self.tiers[:, current_day] = self.tiers[:, prev_day]

        bt.logging.info("Downtime handling complete.")

    def update_scores(self, predictions, closing_line_odds, results):
        """
        Update the scores for the current day based on predictions, closing line odds, and results.

        Args:
            predictions (np.ndarray): Array of miner predictions.
            closing_line_odds (np.ndarray): Array of closing line odds.
            results (np.ndarray): Array of game results.
        """
        bt.logging.info(f"Updating scores for day {self.current_day}")
        try:
            if predictions.size > 0 and closing_line_odds.size > 0 and results.size > 0:
                self._update_raw_scores(predictions, closing_line_odds, results)
                self._update_composite_scores()
                self.log_score_summary()
            else:
                bt.logging.warning("No data available for score update.")
        except Exception as e:
            bt.logging.error(f"Error updating scores: {str(e)}")
            raise

    def _update_raw_scores(self, predictions, closing_line_odds, results):
        bt.logging.trace(f"Predictions shape: {predictions.shape}")
        bt.logging.trace(f"Closing line odds shape: {closing_line_odds.shape}")
        bt.logging.trace(f"Results shape: {results.shape}")

        # Extract unique external_ids from predictions
        external_ids = np.unique(predictions[:, 1]).astype(int).tolist()
        bt.logging.debug(f"Unique external_ids: {external_ids}")

        # Calculate CLV scores for all predictions
        clv_scores = self._calculate_clv_scores(predictions, closing_line_odds)
        self.clv_scores[:, self.current_day] = clv_scores

        # Calculate ROI scores for all predictions
        roi_scores = self._calculate_roi_scores(predictions, results)
        self.roi_scores[:, self.current_day] = roi_scores

        # Calculate Sortino scores for all predictions
        sortino_scores = self._calculate_sortino_scores(predictions, results)
        self.sortino_scores[:, self.current_day] = sortino_scores

        # Extract miner_id from index 0 and wager from index 5
        for pred in predictions:
            try:
                miner_id = int(pred[0])  
                wager = float(pred[5])  
            except (IndexError, ValueError) as e:
                bt.logging.error(f"Error extracting miner_id or wager: {e}")
                continue  # Skip this prediction if extraction fails

            current_wager = self.amount_wagered[miner_id, self.current_day]
            # bt.logging.debug(
            #    f"Miner {miner_id} - Current Daily Wager: {current_wager}, Incoming Wager: {wager}"
            # )

            if current_wager + wager > 1000:
                capped_wager = 1000 - current_wager
                if capped_wager > 0:
                    bt.logging.warning(
                        f"Capping daily wager for miner {miner_id} on day {self.current_day} to {capped_wager}"
                    )
                    self.amount_wagered[miner_id, self.current_day] += capped_wager
                    # bt.logging.debug(
                    #     f"Miner {miner_id} - Daily Wager updated to: {self.amount_wagered[miner_id, self.current_day]}"
                    # )
                else:
                    bt.logging.warning(
                        f"Daily wager cap reached for miner {miner_id} on day {self.current_day}. Wager not added."
                    )
            else:
                self.amount_wagered[miner_id, self.current_day] += wager
                # bt.logging.debug(
                #     f"Miner {miner_id} - Daily Wager updated to: {self.amount_wagered[miner_id, self.current_day]}"
                # )

        # Update entropy scores
        entropy_scores = self.entropy_system.get_current_ebdr_scores(
            self.current_date, self.current_day, external_ids
        )
        self._set_array_for_day(self.entropy_scores, self.current_day, entropy_scores)

        bt.logging.info(
            f"Entropy scores - min: {entropy_scores.min():.8f}, "
            f"max: {entropy_scores.max():.8f}, "
            f"mean: {entropy_scores.mean():.8f}, "
            f"non-zero: {(entropy_scores != 0).sum()}"
        )

    def _calculate_clv_scores(self, predictions, closing_line_odds):
        """
        Calculate Closing Line Value (CLV) scores for miners.

        Args:
            predictions (np.ndarray): Structured data with shape (num_predictions, 6).
            closing_line_odds (np.ndarray): Closing line odds with shape (num_games, 3).

        Returns:
            np.ndarray: CLV scores with shape (num_miners,).
        """
        if predictions.size == 0 or closing_line_odds.size == 0:
            bt.logging.error("Predictions or closing line odds are empty.")
            return np.zeros(self.num_miners)

        clv_scores = np.zeros(self.num_miners)
        prediction_counts = np.zeros(self.num_miners)

        # Create a mapping from external_id to index in closing_line_odds
        unique_external_ids = np.unique(predictions[:, 1])
        external_id_to_index = {external_id: idx for idx, external_id in enumerate(unique_external_ids)}

        for pred in predictions:
            miner_id, external_id, predicted_outcome, predicted_odds, payout, wager = pred
            miner_id = int(miner_id)
            external_id = int(external_id)
            predicted_outcome = int(predicted_outcome)

            if 0 <= miner_id < self.num_miners:
                if (
                    external_id in external_id_to_index
                    and predicted_outcome < closing_line_odds.shape[1]
                ):
                    closing_odds_index = external_id_to_index[external_id]
                    closing_odds = closing_line_odds[
                        closing_odds_index, predicted_outcome
                    ]

                    if closing_odds > 0:
                        clv = predicted_odds / closing_odds
                        if np.isfinite(clv):
                            clv_scores[miner_id] += clv
                            prediction_counts[miner_id] += 1
                        else:
                            bt.logging.warning(
                                f"Invalid CLV value for miner {miner_id} on external_id {external_id}."
                            )
                    elif predicted_outcome == 2:
                        continue  # Tie outcome
                        # bt.logging.trace(f"No tie odds for game {game_id}. Skipping CLV calculation for this prediction.")
                    else:
                        bt.logging.warning(
                            f"Closing odds are zero for external_id {external_id}, outcome {predicted_outcome}."
                        )
                else:
                    bt.logging.warning(
                        f"Invalid external_id or predicted_outcome: {external_id}, {predicted_outcome}"
                    )
            else:
                bt.logging.warning(
                    f"Invalid miner_id {miner_id} encountered. Skipping this prediction."
                )

        # Avoid division by zero and compute average CLV per miner
        mask = prediction_counts > 0
        clv_scores[mask] /= prediction_counts[mask]

        return clv_scores

    def _calculate_roi_scores(self, predictions, results):
        """
        Calculate Return on Investment (ROI) scores for miners.

        Args:
            predictions (np.ndarray): Structured prediction data with shape (num_predictions, 6).
            results (np.ndarray): Array of game results with shape (num_games, 2).

        Returns:
            np.ndarray: ROI scores with shape (num_miners,), representing percentage returns.
        """
        if predictions.size == 0 or results.size == 0:
            bt.logging.error("Predictions or game results are empty.")
            return np.zeros(self.num_miners)

        roi_scores = np.zeros(self.num_miners)
        prediction_counts = np.zeros(self.num_miners)

        # Create a dictionary mapping external_id to outcome
        game_outcomes = dict(results)

        for pred in predictions:
            miner_id, game_id, predicted_outcome, predicted_odds, payout, wager = pred
            miner_id = int(miner_id)
            game_id = int(game_id)

            # Fetch the actual outcome
            actual_outcome = game_outcomes.get(game_id)
            if actual_outcome is None:
                bt.logging.error(
                    f"No actual outcome found for game_id {game_id}. Skipping ROI calculation for miner {miner_id}."
                )
                continue

            if wager == 0:
                bt.logging.error(
                    f"Wager is zero for miner {miner_id} on game_id {game_id}. Skipping ROI calculation."
                )
                continue

            roi = (payout - wager) / wager  # ROI as a percentage

            # bt.logging.debug(
            #     f"Miner {miner_id} | Game ID (External ID) {game_id} | Predicted Outcome: {predicted_outcome} | "
            #     f"Actual Outcome: {actual_outcome} | Wager: {wager} | Payout: {payout} | ROI: {roi}"
            # )

            if np.isfinite(roi):
                roi_scores[miner_id] += roi
                prediction_counts[miner_id] += 1
            else:
                bt.logging.error(
                    f"Invalid ROI value ({roi}) for miner {miner_id} on game_id {game_id}."
                )

        # Compute average ROI per miner without normalization
        mask = prediction_counts > 0
        roi_scores[mask] /= prediction_counts[mask]

        bt.logging.info(
            f"ROI Scores - min: {roi_scores.min():.4f}, max: {roi_scores.max():.4f}, mean: {roi_scores.mean():.4f}"
        )

        return roi_scores

    def _calculate_sortino_scores(self, predictions, results):
        """
        Calculate Sortino ratios for miners based on individual prediction outcomes.

        Args:
            predictions (np.ndarray): Structured prediction data with shape (num_predictions, 6).
            results (np.ndarray): Array of game results with shape (num_games, 2).

        Returns:
            np.ndarray: Sortino ratios with shape (num_miners,).
        """
        if predictions.size == 0 or results.size == 0:
            bt.logging.error("Predictions or game results are empty.")
            return np.zeros(self.num_miners)

        sortino_ratios = np.zeros(self.num_miners)
        returns = np.zeros(self.num_miners, dtype=object)
        for miner_id in range(self.num_miners):
            returns[miner_id] = []

        game_outcomes = dict(results)

        risk_free_rate = 0.00  # Adjust as needed
        max_sortino_ratio = 10.0  # Set a maximum cap for Sortino ratio
        epsilon = 0.01  # Increased epsilon to avoid division by very small numbers

        for pred in predictions:
            miner_id, game_id, predicted_outcome, predicted_odds, payout, wager = pred
            miner_id = int(miner_id)
            game_id = int(game_id)

            actual_outcome = game_outcomes.get(game_id)
            if actual_outcome is None:
                bt.logging.error(f"No actual outcome found for game_id {game_id}. Skipping this prediction.")
                continue

            if wager == 0:
                bt.logging.error(f"Wager is zero for miner {miner_id} on game_id {game_id}. Skipping this prediction.")
                continue

            return_rate = (payout - wager) / wager - risk_free_rate
            returns[miner_id].append(return_rate)

        for miner_id in range(self.num_miners):
            miner_returns = np.array(returns[miner_id])
            if len(miner_returns) > 0:
                # Remove outliers (returns more than 3 standard deviations from the mean)
                mean_return = np.mean(miner_returns)
                std_return = np.std(miner_returns)
                miner_returns = miner_returns[abs(miner_returns - mean_return) <= 3 * std_return]

                if len(miner_returns) > 0:
                    average_return = np.mean(miner_returns)
                    downside_returns = miner_returns[miner_returns < 0]
                    if len(downside_returns) > 0:
                        downside_deviation = np.sqrt(np.mean(np.square(downside_returns)))
                        sortino_ratio = average_return / (downside_deviation + epsilon)
                    else:
                        # Use Sharpe ratio instead when all returns are positive
                        sortino_ratio = average_return / (np.std(miner_returns) + epsilon)
                    
                    # Cap the Sortino ratio
                    sortino_ratio = min(sortino_ratio, max_sortino_ratio)
                    sortino_ratios[miner_id] = sortino_ratio

        return np.nan_to_num(sortino_ratios, nan=0.0)

    def _update_composite_scores(self):
        clv = self.clv_scores[:, self.current_day]
        roi = self.roi_scores[:, self.current_day]
        entropy = self.entropy_scores[:, self.current_day]
        sortino = self.sortino_scores[:, self.current_day]

        # Calculate daily composite scores without normalizing any component scores
        daily_composite_scores = (
            self.clv_weight * clv
            + self.roi_weight * roi
            + self.sortino_weight * sortino
            + self.entropy_weight * entropy
        )

        # Update the daily composite scores (index 0 of the 3rd dimension)
        self.composite_scores[:, self.current_day, 0] = daily_composite_scores

        # Calculate rolling averages for each tier based on raw composite scores
        for tier in range(1, 6):  # Tiers 1 to 5
            window = self.tier_configs[tier + 1]["window"]  # +1 because tier configs are 0-indexed
            start_day = (self.current_day - window + 1) % self.max_days

            if start_day <= self.current_day:
                window_scores = self.composite_scores[:, start_day : self.current_day + 1, 0]
            else:
                window_scores = np.concatenate(
                    [
                        self.composite_scores[:, start_day:, 0],
                        self.composite_scores[:, : self.current_day + 1, 0],
                    ],
                    axis=1,
                )

            rolling_avg = np.mean(window_scores, axis=1)
            self.composite_scores[:, self.current_day, tier] = rolling_avg

        bt.logging.debug(f"Composite scores for day {self.current_day}:")
        bt.logging.debug(
            f"CLV: min={clv.min():.4f}, max={clv.max():.4f}, mean={clv.mean():.4f}"
        )
        bt.logging.debug(
            f"ROI: min={roi.min():.4f}, max={roi.max():.4f}, mean={roi.mean():.4f}"
        )
        bt.logging.debug(
            f"Sortino: min={sortino.min():.4f}, max={sortino.max():.4f}, mean={sortino.mean():.4f}"
        )
        bt.logging.debug(
            f"Entropy: min={entropy.min():.4f}, max={entropy.max():.4f}, mean={entropy.mean():.4f}"
        )
        bt.logging.debug(
            f"Daily Composite: min={daily_composite_scores.min():.4f}, max={daily_composite_scores.max():.4f}, mean={daily_composite_scores.mean():.4f}"
        )

    def _promote_and_swap(self, tiers, composite_scores_day):
        """
        Promote miners to higher tiers and perform swaps when necessary, ensuring min_wager is respected.

        Args:
            tiers (np.ndarray): The current tiers of all miners.
            composite_scores_day (np.ndarray): The composite scores of all miners for the current day across all tiers.
                                               Shape: [miners, tiers]
        """
        for tier in range(
            2, self.num_tiers - 1
        ):  # Start from tier 1 up to the second-highest tier
            current_tier_miners = np.where(tiers == tier)[0]
            next_tier_miners = np.where(tiers == tier + 1)[0]

            # Check if there are open slots in the next tier
            open_slots = self.tier_configs[tier + 1]["capacity"] - len(next_tier_miners)

            if open_slots > 0:
                # Identify eligible miners for promotion based on min_wager
                eligible_miners = [
                    miner
                    for miner in current_tier_miners
                    if self._meets_tier_requirements(miner, tier + 1)
                ]
                bt.logging.debug(
                    f"Tier {tier}: Eligible miners for promotion: {eligible_miners}"
                )

                # Sort eligible miners by composite scores descending
                eligible_miners_sorted = sorted(
                    eligible_miners,
                    key=lambda x: composite_scores_day[x, tier - 1],
                    reverse=True,
                )

                # Promote up to the number of open slots
                for miner in eligible_miners_sorted[:open_slots]:
                    tiers[miner] = tier + 1
                    bt.logging.info(f"Miner {miner} promoted to tier {tier }")

            else:
                # If no open slots, consider swapping
                if len(next_tier_miners) > 0:
                    # Sort current tier miners by composite score ascending (lowest first)
                    sorted_current = sorted(
                        current_tier_miners,
                        key=lambda x: composite_scores_day[x, tier - 1],
                    )
                    # Sort next tier miners by composite score ascending
                    sorted_next = sorted(
                        next_tier_miners, key=lambda x: composite_scores_day[x, tier]
                    )

                    for promoting_miner, demoting_miner in zip(
                        sorted_current, sorted_next
                    ):
                        promoting_score = composite_scores_day[
                            promoting_miner, tier - 1
                        ]
                        demoting_score = composite_scores_day[demoting_miner, tier]

                        if (
                            promoting_score > demoting_score
                            and self._meets_tier_requirements(promoting_miner, tier + 1)
                        ):
                            # Swap tiers
                            tiers[promoting_miner], tiers[demoting_miner] = (
                                tiers[demoting_miner],
                                tiers[promoting_miner],
                            )
                            bt.logging.info(
                                f"Swapped miner {promoting_miner} (promoted to tier {tier + 1}) "
                                f"with miner {demoting_miner} (demoted to tier {tier})"
                            )
                        else:
                            bt.logging.debug(
                                f"No swap needed between miner {promoting_miner} and miner {demoting_miner}"
                            )
                            break  # Stop if no further swaps are beneficial

    def _meets_tier_requirements(self, miner, tier):
        """
        Check if a miner meets the requirements for a given tier based on min_wager.

        Args:
            miner (int): The miner's UID.
            tier (int): The tier to check requirements for.

        Returns:
            bool: True if the miner meets the tier requirements, False otherwise.
        """
        config = self.tier_configs[tier]
        cumulative_wager = self._get_cumulative_wager(miner, config["window"])
        meets_requirement = cumulative_wager >= config["min_wager"] 

        if meets_requirement:
            pass
            # bt.logging.debug(f"Miner {miner} meets tier {tier-1} requirements with cumulative wager {cumulative_wager}")
        else:
            pass
            # bt.logging.debug(f"Miner {miner} does NOT meet tier {tier-1} requirements with cumulative wager {cumulative_wager}")

        return meets_requirement

    def manage_tiers(self,invalid_uids,valid_uids):
        bt.logging.info("Managing tiers")

        try:
            current_tiers = self.tiers[:, self.current_day].copy()
            bt.logging.debug(
                f"Composite Scores Shape: {self.composite_scores.shape}"
            )  # Debug statement
            bt.logging.info(
                f"Current tiers before management: {np.bincount(current_tiers, minlength=self.num_tiers)}"
            )

            bt.logging.info(f"Assigned {len(self.empty_uids)} empty slots")

            # Step 1: Check for and perform demotions
            for tier in range(
                6, 1, -1
            ):  # Start from tier 5 (index 6), go to tier 1 (index 2)
                tier_miners = np.where(current_tiers == tier)[0]
                for miner in tier_miners:
                    if not self._meets_tier_requirements(miner, tier):
                        self._cascade_demotion(
                            miner, tier, current_tiers, self.composite_scores
                        )

            # Step 2: Promote and swap
            # Pass the entire slice for the current day across all tiers
            composite_scores_day = self.composite_scores[:, self.current_day, :]
            self._promote_and_swap(current_tiers, composite_scores_day)

            # Step 3: Fill empty slots
            for tier in range(2, self.num_tiers):  # Start from tier 2
                # Pass the composite scores specific to the tier being filled
                composite_score_tier = self.composite_scores[
                    :, self.current_day, tier - 1
                ]
                self._fill_empty_slots(
                    tier, current_tiers, composite_score_tier, self.tier_configs[tier]
                )

            # Update tiers for the current day
            self.tiers[:, self.current_day] = current_tiers

            # Set invalid UIDs to tier 0
            self.tiers[list(self.invalid_uids), self.current_day] = 0

            bt.logging.info(
                f"Current tiers after management: {np.bincount(current_tiers, minlength=self.num_tiers)}"
            )
            bt.logging.info("Tier management completed")


        except Exception as e:
            bt.logging.error(f"Error managing tiers: {str(e)}")
            raise


    def _fill_empty_slots(
        self, tier, current_tiers, composite_scores_tier, tier_config
    ):
        """
        Fill empty slots in a tier with eligible miners.

        Args:
            tier (int): The tier to fill slots in.
            current_tiers (np.ndarray): The current tiers of all miners.
            composite_scores_tier (np.ndarray): The composite scores of all miners for the current tier on the current day.
                                               Shape: [miners]
            tier_config (dict): Configuration for the tier.
        """
        # Ignore tiers 0 and 1
        if tier <= 1:
            return

        required_slots = tier_config["capacity"]
        current_miners = np.where(current_tiers == tier)[0]
        open_slots = required_slots - len(current_miners)

        if open_slots > 0:
            # Identify eligible miners from all lower valid tiers (2 to tier - 1)
            lower_tier_miners = np.where((current_tiers >= 2) & (current_tiers < tier))[0]
            eligible_miners = [
                miner
                for miner in lower_tier_miners
                if self._meets_tier_requirements(miner, tier)
                and miner not in current_miners
            ]

            # Sort eligible miners by their composite scores in descending order
            sorted_eligible = sorted(
                eligible_miners, key=lambda m: composite_scores_tier[m], reverse=True
            )

            # Promote top eligible miners to fill the open slots
            for miner in sorted_eligible[:open_slots]:
                current_tiers[miner] = tier
                bt.logging.info(
                    f"Miner {miner} promoted to tier {tier - 1} from tier {current_tiers[miner] - 1} to fill empty slot"
                )

    def _get_cumulative_wager(self, miner, window):
        end_day = self.current_day
        start_day = end_day - window + 1

        if start_day >= 0:
            wager = self.amount_wagered[miner, start_day : end_day + 1].sum()
        else:
            # Wrap around the circular buffer
            wager = (
                self.amount_wagered[miner, start_day:].sum()
                + self.amount_wagered[miner, : end_day + 1].sum()
            )

        return wager

    def _cascade_demotion(self, miner, current_tier, tiers, composite_scores):
        """
        Demote a miner from the current tier to the next lower tier and handle further demotions if necessary.

        Args:
            miner (int): The miner's UID.
            current_tier (int): The current tier of the miner.
            tiers (np.ndarray): The array representing current tiers of all miners.
            composite_scores (np.ndarray): The composite scores array for the current day.
        """
        bt.logging.debug(f"Demoting miner {miner} from tier {current_tier-1}")

        # Demote to the next lower tier
        new_tier = current_tier - 1
        if new_tier >= 1:
            tiers[miner] = new_tier
            # bt.logging.info(f"Miner {miner} demoted to tier {new_tier-1}")

            # Check if the miner still meets the min_wager for the new tier
            if not self._meets_tier_requirements(miner, new_tier):
                self._cascade_demotion(miner, new_tier, tiers, composite_scores)
        else:
            # Set to tier 0 if below the lowest tier
            tiers[miner] = 0
            bt.logging.info(f"Miner {miner} demoted to tier 0 (lowest tier)")

    def reset_miner(self, miner_uid: int):
        """
        Initialize a new miner, replacing any existing miner at the same UID.

        Args:
            miner_uid (int): The UID of the miner to reset.
        """
        self.clv_scores[miner_uid] = 0
        self.roi_scores[miner_uid] = 0
        self.amount_wagered[miner_uid] = 0
        self.composite_scores[miner_uid] = 0
        self.entropy_scores[miner_uid] = 0
        self.tiers[miner_uid] = 1 if miner_uid in self.invalid_uids else 2

    def get_miner_history(self, miner_uid: int, score_type: str, days: int = None):
        """
        Get the score history for a specific miner and score type.

        Args:
            miner_uid (int): The UID of the miner.
            score_type (str): The type of score to retrieve ('clv', 'roi', 'sortino', 'entropy', 'composite', 'tier').
            days (int, optional): Number of days of history to return. If None, returns all available history.

        Returns:
            np.ndarray: An array containing the miner's score history.
        """
        score_array = getattr(self, f"{score_type}_scores", None)
        if score_array is None:
            raise ValueError(f"Invalid score type: {score_type}")

        if days is None:
            return score_array[miner_uid]
        else:
            return score_array[miner_uid, -days:]

    def log_score_summary(self):
        """
        Log a summary of the current scores.
        """
        bt.logging.info("=== Score Summary ===")
        for score_name, score_array in [
            ("CLV", self.clv_scores),
            ("ROI", self.roi_scores),
            ("Entropy", self.entropy_scores),
            ("Sortino", self.sortino_scores),
            ("Composite", self.composite_scores),
            
        ]:
            current_scores = self._get_array_for_day(score_array, self.current_day)
            bt.logging.info(
                f"{score_name} Scores - min: {current_scores.min():.4f}, "
                f"max: {current_scores.max():.4f}, "
                f"mean: {current_scores.mean():.4f}, "
                f"non-zero: {np.count_nonzero(current_scores)}"
            )

    def calculate_weights(self):
        """
        Calculate weights for all miners based on their tier and composite score.
        Weights sum to 1 and represent both the miner's share of incentives and their influence.

        Returns:
            np.ndarray: The calculated weights for all miners
        """
        bt.logging.info("Calculating weights")

        try:
            weights = np.zeros(self.num_miners)

            tier_incentives = np.array(
                [config["incentive"] for config in self.tier_configs[2:]]
            )  # Exclude tiers -1 and 0
            total_incentive = tier_incentives.sum()
            normalized_incentives = (
                tier_incentives / total_incentive
                if total_incentive > 0
                else np.zeros_like(tier_incentives)
            )

            current_tiers = self.tiers[:, self.current_day]

            # Only consider miners in valid tiers (2 to num_tiers - 1) and not in invalid_uids
            valid_miners = np.array(
                list(set(range(self.num_miners)) - self.invalid_uids)
            )
            valid_miners = valid_miners[
                (current_tiers[valid_miners] >= 2)
                & (current_tiers[valid_miners] < self.num_tiers)
            ]

            # Check if valid_miners is empty
            if not valid_miners.any():
                bt.logging.warning("Scoring.py | calculate_weights | No valid miners found. Returning zero weights.")
                return np.zeros(self.num_miners)

            tier_counts = np.bincount(
                current_tiers[valid_miners], minlength=self.num_tiers
            )[2:]

            # Only consider non-empty tiers
            non_empty_tiers = tier_counts > 0
            active_tiers = np.arange(2, self.num_tiers)[non_empty_tiers]

            # Assign weights based on incentives and composite scores
            for idx, tier in enumerate(active_tiers):
                # Get miners in the current tier
                tier_miners = np.where(current_tiers == tier)[0]
                if len(tier_miners) == 0:
                    continue

                # Normalize composite scores within the tier
                composite_scores = self.composite_scores[tier_miners, self.current_day, 0]
                max_score = composite_scores.max()
                if max_score == 0:
                    normalized_scores = np.ones_like(composite_scores)
                else:
                    normalized_scores = composite_scores / max_score

                # Apply tier incentive to the weights
                incentive_factor = normalized_incentives[idx]
                weights[tier_miners] = normalized_scores * incentive_factor

            # Normalize the weights to sum to 1
            total_weight = weights.sum()
            if total_weight > 0:
                weights /= total_weight
            else:
                bt.logging.warning("Total weight is zero. Distributing weights equally among valid miners.")
                weights[list(valid_miners)] = 1 / len(valid_miners)

            bt.logging.info(f"Weight sum: {weights.sum():.6f}")
            bt.logging.info(
                f"Min weight: {weights.min():.6f}, Max weight: {weights.max():.6f}"
            )
            # Optionally, log individual weights or statistics here

            return weights

        except Exception as e:
            bt.logging.error(f"Error calculating weights: {str(e)}")
            raise

    def scoring_run(self, date, invalid_uids, valid_uids):
        bt.logging.info(f"=== Starting scoring run for date: {date} ===")

        # Update invalid, valid, and empty UIDs
        self.invalid_uids = set(invalid_uids)
        self.valid_uids = set(valid_uids)
        self.empty_uids = set(range(self.num_miners)) - self.valid_uids - self.invalid_uids

        # Create boolean masks for each category
        empty_mask = np.zeros(self.num_miners, dtype=bool)
        empty_mask[list(self.empty_uids)] = True

        invalid_mask = np.zeros(self.num_miners, dtype=bool)
        invalid_mask[list(self.invalid_uids)] = True

        valid_mask = np.zeros(self.num_miners, dtype=bool)
        valid_mask[list(self.valid_uids)] = True

        # Set tiers using boolean masks
        if self.init:
            self.tiers[:, self.current_day] = 2  # Default to tier 2 for valid UIDs, only set if no previous state exists
            self.init = False

        self.tiers[empty_mask, self.current_day] = 0
        self.tiers[invalid_mask, self.current_day] = 1

        bt.logging.info(f"Assigned {len(self.empty_uids)} empty slots to tier 0.")
        bt.logging.info(f"Assigned {len(self.invalid_uids)} invalid UIDs to tier 1.")
        bt.logging.info(f"Assigned {len(self.valid_uids)} valid UIDs to tier 2.")

        current_date = self._ensure_datetime(date)
        self.advance_day(current_date)

        date_str = current_date.isoformat()

        bt.logging.info(
            f"Current day: {self.current_day}, reference date: {self.reference_date}"
        )

        # Add this debugging code before calculating composite scores
        current_tiers = self.tiers[:, self.current_day]
        tier_distribution = [
            int(np.sum(current_tiers == tier))
            for tier in range(1, len(self.tier_configs) + 1)
        ]
        bt.logging.info(f"Current tier distribution: {tier_distribution}")

        (
            predictions,
            closing_line_odds,
            results,
        ) = self.scoring_data.preprocess_for_scoring(date_str)

        bt.logging.info(
            f"Number of predictions: {predictions.shape[0] if predictions.size > 0 else 0}"
        )

        if predictions.size > 0 and closing_line_odds.size > 0 and results.size > 0:
            bt.logging.info("Updating scores...")
            self.update_scores(predictions, closing_line_odds, results)
            bt.logging.info("Scores updated successfully.")

            total_wager = self.amount_wagered[:, self.current_day].sum()
            avg_wager = total_wager / self.num_miners
            bt.logging.info(f"Total wager for this run: {total_wager:.2f}")
            bt.logging.info(f"Average wager per miner: {avg_wager:.2f}")
        else:
            bt.logging.warning(
                f"No predictions for date {date_str}. Skipping score update."
            )

        self.manage_tiers(self.invalid_uids, self.valid_uids)

        # Calculate weights using the existing method
        weights = self.calculate_weights()

        # Set weights for invalid UIDs to zero
        weights[list(self.invalid_uids)] = 0

        # Renormalize weights
        if weights.sum() > 0:
            weights /= weights.sum()
        else:
            # If all weights are zero, distribute equally among valid UIDs
            weights[list(self.valid_uids)] = 1 / len(self.valid_uids)

        bt.logging.info(f"Weight sum: {weights.sum():.6f}")
        bt.logging.info(
            f"Min weight: {weights.min():.6f}, Max weight: {weights.max():.6f}"
        )
        bt.logging.info(f"Weights: {weights}")
        # Log final tier distribution
        self.log_score_summary()

        bt.logging.info(f"=== Completed scoring run for date: {date_str} ===")

        # Save state at the end of each run
        self.save_state()
        self.scoring_data.update_miner_stats(self.current_day)

        return weights

    def reset_date(self, new_date):
        """
        Reset the scoring system's date for testing purposes.

        Args:
            new_date (datetime): The new date to set as the current date.
        """
        # Ensure new_date is timezone-aware
        if new_date.tzinfo is None:
            new_date = new_date.replace(tzinfo=timezone.utc)

        self.current_date = new_date
        days_since_reference = (new_date - self.reference_date).days
        self.current_day = days_since_reference % self.max_days

        # Reset tiers to 1 for the current day, using modulo for wraparound
        self.tiers[:, self._get_day_index(self.current_day)] = 1

    def reset_all_miners_to_tier_1(self):
        """
        Reset all miners to tier 1.
        """
        self.tiers.fill(1)

    def _get_day_index(self, day):
        """
        Get the index of a day in the circular buffer.

        Args:
            day (int): The day to get the index for.

        Returns:
            int: The index of the day in the circular buffer.
        """
        return day % self.max_days

    def _get_array_for_day(self, array, day, tier=None):
        """
        Get the array for a specific day and tier.

        Args:
            array (np.ndarray): The array to get the data from.
            day (int): The day to get the data for.
            tier (int, optional): The tier to get the data for. Defaults to None.

        Returns:
            np.ndarray: The array for the specified day and tier.
        """
        if tier is None:
            return array[:, self._get_day_index(day)]
        else:
            return array[:, self._get_day_index(day), tier]

    def _set_array_for_day(self, array, day, value, tier=None):
        """
        Set the array for a specific day and tier.

        Args:
            array (np.ndarray): The array to set the data in.
            day (int): The day to set the data for.
            value (np.ndarray): The value to set in the array.
            tier (int, optional): The tier to set the data for. Defaults to None.
        """
        if tier is None:
            array[:, self._get_day_index(day)] = value
        else:
            array[:, self._get_day_index(day), tier] = value

    def save_state(self):
        """
        Save the current state of the ScoringSystem to the database.
        """
        try:
            self.db_manager.begin_transaction()

            # Serialize invalid_uids and valid_uids
            invalid_uids_json = json.dumps(list(int(uid) for uid in self.invalid_uids))
            valid_uids_json = json.dumps(list(int(uid) for uid in self.valid_uids))

            # Insert or update the latest state in score_state table
            insert_state_query = """
                INSERT INTO score_state (current_day, current_date, reference_date, invalid_uids, valid_uids, last_update_date)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_id) DO UPDATE SET
                    current_day=excluded.current_day,
                    current_date=excluded.current_date,
                    reference_date=excluded.reference_date,
                    invalid_uids=excluded.invalid_uids,
                    valid_uids=excluded.valid_uids,
                    last_update_date=excluded.last_update_date
            """
            params = (
                self.current_day,
                self.current_date.isoformat() if self.current_date else None,
                self.reference_date.isoformat(),
                invalid_uids_json,
                valid_uids_json,
                self.last_update_date.isoformat() if self.last_update_date else None
            )

            self.db_manager.execute_query(insert_state_query, params)

            # Now save scores
            self.save_scores()

            self.db_manager.commit_transaction()
            bt.logging.info("ScoringSystem state saved to database.")

        except Exception as e:
            self.db_manager.rollback_transaction()
            bt.logging.error(f"Error saving state to database: {e}")
            raise

    def save_scores(self):
        num_miners, num_days, num_scores = self.composite_scores.shape
        bt.logging.info(f"Saving scores for {num_miners} miners, {num_days} days, {num_scores} scores (1 daily + 5 tiers)")
        
        score_records = []
        for miner in range(num_miners):
            for day in range(num_days):
                clv = self.clv_scores[miner, day]
                roi = self.roi_scores[miner, day]
                entropy = self.entropy_scores[miner, day]
                sortino = self.sortino_scores[miner, day]
                for score_index in range(num_scores):
                    score_type = self.tier_mapping[score_index]
                    composite = self.composite_scores[miner, day, score_index]
                    score_records.append((
                        miner,
                        day,
                        score_type,
                        clv,
                        roi,
                        entropy,
                        composite,
                        sortino
                    ))
        
        # Batch insert using executemany
        insert_score_query = """
           INSERT OR REPLACE INTO scores 
           (miner_uid, day_id, score_type, clv_score, roi_score, entropy_score, composite_score, sortino_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db_manager.executemany(insert_score_query, score_records)
        bt.logging.info(f"Saved {len(score_records)} score records")

    def load_state(self):
        """
        Load the state of the ScoringSystem from the database.
        """
        try:
            # Fetch the latest state from score_state table
            fetch_state_query = """
                SELECT current_day, current_date, reference_date, invalid_uids, valid_uids, last_update_date
                FROM score_state
                ORDER BY state_id DESC
                LIMIT 1
            """
            state = self.db_manager.fetch_one(fetch_state_query, None)
            if state:
                self.current_day = state["current_day"]
                self.current_date = datetime.fromisoformat(state["current_date"]) if state["current_date"] else None
                if self.current_date and self.current_date.tzinfo is None:
                    self.current_date = self.current_date.replace(tzinfo=timezone.utc)
                self.reference_date = datetime.fromisoformat(state["reference_date"])
                if self.reference_date.tzinfo is None:
                    self.reference_date = self.reference_date.replace(tzinfo=timezone.utc)
                self.invalid_uids = set(json.loads(state["invalid_uids"]))
                self.valid_uids = set(json.loads(state["valid_uids"]))
                self.last_update_date = datetime.fromisoformat(state["last_update_date"]) if state["last_update_date"] else None
                if self.last_update_date and self.last_update_date.tzinfo is None:
                    self.last_update_date = self.last_update_date.replace(tzinfo=timezone.utc)
                bt.logging.info("ScoringSystem state loaded from database.")
            else:
                bt.logging.warning("No state found in database. Starting with default state.")
                return True #initial state

            # Load scores
            self.load_scores()
            return False

        except Exception as e:
            bt.logging.error(f"Error loading state from database: {e}")
            raise


    def load_scores(self):
        """
        Load the scores from the database.
        """
        try:
            fetch_scores_query = """
                SELECT miner_uid, day_id, tier_id, clv_score, roi_score, entropy_score, composite_score, sortino_score
                FROM scores
            """
            scores = self.db_manager.fetch_all(fetch_scores_query, None)
            for score in scores:
                miner_uid = score["miner_uid"]
                day_id = score["day_id"]
                tier_id = score["tier_id"]
                self.clv_scores[miner_uid, day_id] = float(score["clv_score"]) if score["clv_score"] is not None else np.nan
                self.roi_scores[miner_uid, day_id] = float(score["roi_score"]) if score["roi_score"] is not None else np.nan
                self.entropy_scores[miner_uid, day_id] = float(score["entropy_score"]) if score["entropy_score"] is not None else np.nan
                self.sortino_scores[miner_uid, day_id] = float(score["sortino_score"]) if score["sortino_score"] is not None else np.nan
                self.composite_scores[miner_uid, day_id, tier_id] = float(score["composite_score"]) if score["composite_score"] is not None else np.nan

            bt.logging.info("Scores loaded from database.")

        except Exception as e:
            bt.logging.error(f"Error loading scores from database: {e}")
            raise

    def _ensure_datetime(self, date):
        if isinstance(date, str):
            return datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
        elif isinstance(date, datetime) and date.tzinfo is None:
            return date.replace(tzinfo=timezone.utc)
        return date

    def reset_miner(self, miner_uid: int):
        """
        Initialize a new miner, replacing any existing miner at the same UID.

        Args:
            miner_uid (int): The UID of the miner to reset.
        """
        self.clv_scores[miner_uid] = 0
        self.roi_scores[miner_uid] = 0
        self.amount_wagered[miner_uid] = 0
        self.composite_scores[miner_uid] = 0
        self.entropy_scores[miner_uid] = 0
        self.tiers[miner_uid] = 1 if miner_uid in self.invalid_uids else 2
