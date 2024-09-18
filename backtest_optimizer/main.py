import sys
import os

# Get the directory of the current script
current_dir = os.path.dirname(os.path.abspath(__file__))

# Add the current directory to the PYTHONPATH
sys.path.append(current_dir)

import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering
from sklearn.compose import make_column_selector
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.compose import ColumnTransformer
import math
from tqdm import tqdm
import optuna
from optuna.pruners import BasePruner
from multiprocessing import Pool, Manager, set_start_method, get_start_method
import itertools
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt
from itertools import combinations
from collections.abc import Iterable
from functools import partial
import logging
import matplotlib
import itertools as itt

from metrics import *
from backtest_stress_tests import run_stress_tests


class RepeatPruner(BasePruner):
    def prune(self, study: optuna.Study, trial: optuna.Trial) -> bool:
        """
        Prune trials that have duplicate parameters.

        Args:
            study (optuna.Study): The study object.
            trial (optuna.Trial): The trial object.

        Returns:
            bool: True if the trial should be pruned, False otherwise.
        """
        print("RepeatPruner is called.")
        trials = study.get_trials(deepcopy=False)
        params_list = [t.params for t in trials]
        if params_list.count(trial.params) > 1:
            print(
                f"Trial {trial.number} pruned due to duplicate parameters: {trial.params}"
            )
            return True
        return False


def standalone_combcv_pl(calc_pl, params: dict, group_dict: dict) -> tuple:
    """
    Calculate performance metrics using combinatorial cross-validation.

    Args:
        calc_pl (callable): Function to calculate performance.
        params (dict): Parameters for the performance calculation.
        group_dict (dict): Dictionary of group data.

    Returns:
        tuple: (final_sharpe, final_returns) calculated metrics.
    """
    final_returns = []
    for group_num, group_data in group_dict.items():
        group_data = group_data.copy()
        returns = calc_pl(group_data, params)
        final_returns.append(returns)

    sharpe_ratios = [
        annual_sharpe(returns) for returns in final_returns if not returns.empty
    ]
    final_sharpe = np.nanmean(sharpe_ratios)
    return final_sharpe, final_returns


def calc_returns(args):
    calc_pl_func, params, train_data = args
    try:
        returns = calc_pl_func(train_data, params)
    except Exception as e:
        logging.info(
            f"PL calculation for stress tests failed for {params}, with error {e}"
        )
        returns = pd.Series()
    if not returns.empty:
        returns = returns.resample("D").sum()
    return returns


class ParameterOptimizer:
    def __init__(self, calc_pl: callable, save_path: str):
        """
        Initialize the parameter optimizer.

        Args:
            calc_pl (callable): Function to calculate performance metrics.
            save_path: path for saving and loading results (fodler)
        """
        self.calc_pl = calc_pl
        self.combcv_dict = {}
        self.params_dict = {}
        self.all_tested_params = []
        self.best_params_by_fold = {}
        self.backtest_paths = {}
        self.top_params_list = None
        self.current_group = None
        self.train_data = {}
        self.test_data = {}
        self.save_path = save_path

    def check_datetime_index_integrity(
        self, data_dict: Dict[str, pd.DataFrame]
    ) -> Tuple[bool, List[str]]:
        """
        Check the integrity of datetime indices in a dictionary of DataFrames.

        Args:
        data_dict (Dict[str, pd.DataFrame]): Dictionary of DataFrames with datetime indices.

        Returns:
        Tuple[bool, List[str]]: A tuple containing:
            - Boolean indicating overall integrity (True if all checks pass)
            - List of error messages (empty if all checks pass)
        """
        error_messages = []

        # Check if dictionary is empty
        if not data_dict:
            error_messages.append("The input dictionary is empty.")
            return False, error_messages

        # Collect all unique dates across all DataFrames
        all_dates = set()
        for ticker, df in data_dict.items():
            # Check if index is DatetimeIndex
            if not isinstance(df.index, pd.DatetimeIndex):
                error_messages.append(
                    f"DataFrame for {ticker} does not have a DatetimeIndex."
                )
                continue

            all_dates.update(df.index)

        all_dates = sorted(all_dates)

        for ticker, df in data_dict.items():
            # Check for duplicate indices
            if df.index.duplicated().any():
                error_messages.append(
                    f"DataFrame for {ticker} contains duplicate timestamps."
                )

            # Check if index is sorted
            if not df.index.is_monotonic_increasing:
                error_messages.append(
                    f"Index for {ticker} is not monotonically increasing."
                )

            # Check for missing dates
            missing_dates = set(all_dates) - set(df.index)
            if missing_dates:
                error_messages.append(
                    f"DataFrame for {ticker} is missing {len(missing_dates)} dates."
                )

            # Check for gaps in the index
            if len(df) > 1:
                freq = pd.infer_freq(df.index)
                if freq is not None:
                    ideal_index = pd.date_range(
                        start=df.index.min(), end=df.index.max(), freq=freq
                    )
                    gaps = ideal_index.difference(df.index)
                    if len(gaps) > 0:
                        error_messages.append(
                            f"DataFrame for {ticker} has {len(gaps)} gaps in its index."
                        )
                else:
                    error_messages.append(
                        f"Unable to infer consistent frequency for {ticker}. Cannot check for gaps."
                    )

            # Check for future dates
            if df.index.max() > pd.Timestamp.now():
                error_messages.append(f"DataFrame for {ticker} contains future dates.")

            # Check for very old dates (e.g., before year 2000)
            if df.index.min() < pd.Timestamp("2000-01-01"):
                error_messages.append(
                    f"DataFrame for {ticker} contains very old dates (before year 2000)."
                )

        return len(error_messages) == 0, error_messages

    def align_dataframes_to_max_index(self, data_dict: dict):
        # Find the maximum date range
        all_dates = pd.DatetimeIndex([])
        for df in data_dict.values():
            all_dates = all_dates.union(df.index)

        # Sort the dates to ensure they're in chronological order
        all_dates = all_dates.sort_values()

        # Create a new dictionary to store the aligned DataFrames
        aligned_data_dict = {}

        for ticker, df in data_dict.items():
            # Reindex the DataFrame to the full date range
            aligned_df = df.reindex(all_dates)

            # If you want to forward fill a limited number of NaNs (e.g., 5 days), uncomment the next line
            # aligned_df = aligned_df.fillna(method='ffill', limit=5)

            aligned_data_dict[ticker] = aligned_df

        return aligned_data_dict

    def split_data(self, data_dict: dict, train_end: str):
        """
        Split data into training and testing sets based on the specified date.

        Args:
            data_dict (dict): Dictionary containing the data.
            train_end (str): The end date for the training data.
        """
        logging.info(f"Splitting data to train-test, cutoff: {train_end}")

        data_dict = self.align_dataframes_to_max_index(data_dict)

        for ticker, df in data_dict.items():

            for col in df.select_dtypes(include=[np.float64]).columns:
                df[col] = df[col].astype(np.float32)
            for col in df.select_dtypes(include=[np.int64]).columns:
                df[col] = df[col].astype(np.int32)

            train_df = df.loc[:train_end].copy()
            if not train_df.empty:
                self.train_data[ticker] = train_df
            else:
                continue

            if train_end in df.index:
                test_df = df.loc[train_end:].copy()
                if not test_df.empty:
                    self.test_data[ticker] = test_df

        logging.info(f"Successfully splitted data")

    def cpcv_generator(
        self, t_span: int, n: int, k: int, verbose: bool = True
    ) -> tuple:
        """
        Generate combinatorial purged cross-validation (CPCV) splits.

        Args:
            t_span (int): Total time span.
            n (int): Number of groups.
            k (int): Number of test groups.
            verbose (bool): Whether to print information about the splits.

        Returns:
            tuple: (is_test, paths, path_folds) arrays for CPCV.
        """
        group_num = np.arange(t_span) // (t_span // n)
        group_num[group_num == n] = n - 1

        test_groups = np.array(list(itt.combinations(np.arange(n), k))).reshape(-1, k)
        C_nk = len(test_groups)
        n_paths = C_nk * k // n

        if verbose:
            print("n_sim:", C_nk)
            print("n_paths:", n_paths)

        is_test_group = np.full((n, C_nk), fill_value=False)
        is_test = np.full((t_span, C_nk), fill_value=False)

        if k > 1:
            for k, pair in enumerate(test_groups):
                for i in pair:
                    is_test_group[i, k] = True
                    mask = group_num == i
                    is_test[mask, k] = True
        else:
            for k, i in enumerate(test_groups.flatten()):
                is_test_group[i, k] = True
                mask = group_num == i
                is_test[mask, k] = True

        path_folds = np.full((n, n_paths), fill_value=np.nan)

        for i in range(n_paths):
            for j in range(n):
                s_idx = is_test_group[j, :].argmax().astype(int)
                path_folds[j, i] = s_idx
                is_test_group[j, s_idx] = False

        paths = np.full((t_span, n_paths), fill_value=np.nan)

        for p in range(n_paths):
            for i in range(n):
                mask = group_num == i
                paths[mask, p] = int(path_folds[i, p])

        return (is_test, paths, path_folds)

    def generate_group_dict(self, is_train: bool) -> dict:

        group_dict = {}
        for ticker, df in self.train_data.items():
            if ticker not in self.current_group:
                continue
            df = df.copy()
            select_idx = self.current_group[ticker]["train"]
            if self.current_group[ticker]["test"] and not is_train:
                select_idx = self.current_group[ticker]["test"]
            for i, idx in enumerate(select_idx):
                if i not in group_dict:
                    group_dict[i] = {}
                new_df = df.iloc[idx]
                if not new_df.empty:
                    group_dict[i][ticker] = new_df

        logging.info("Checking datetime integrity for tickers")
        for i, data_dict in group_dict.items():
            integrity_check, messages = self.check_datetime_index_integrity(data_dict)
            if not integrity_check:
                for message in messages:
                    print(message)

        return group_dict

    def combcv_pl(self, params: dict, group_dict: dict) -> tuple:
        """
        Calculate performance metrics using combinatorial cross-validation.

        Args:
            params (dict): Parameters for the performance calculation.
            is_train (bool): Whether the data is for training or testing.

        Returns:
            tuple: (final_sharpe, final_returns) calculated metrics.
        """
        final_returns = []
        for group_num, group_data in group_dict.items():
            returns = self.calc_pl(group_data, params)
            final_returns.append(returns)

        sharpe_ratios = [
            annual_sharpe(returns) for returns in final_returns if not returns.empty
        ]
        final_sharpe = np.nanmean(sharpe_ratios)
        return final_sharpe, final_returns

    def plot_returns(self, data_dict, params: dict):
        """
        Calculate out-of-sample Sharpe ratio and plot cumulative returns.

        Args:
            data_dict: dictionary with data in format {'ticker':DF,...}
            params (dict): Parameters for the performance calculation.
        """

        logging.info("Plotting results")

        returns = self.calc_pl(data_dict, params)
        metrics = calculate_metrics(returns)
        text = ""
        for metric, v in metrics.items():
            text += f"{metric}: {round(v, 2)}\n"

        fig, ax = plt.subplots(figsize=(12, 8))
        ax.plot(returns.cumsum())
        props = dict(boxstyle="round", facecolor="wheat", alpha=0.5)
        ax.text(
            0.05,
            0.95,
            text,
            transform=ax.transAxes,
            fontsize=12,
            verticalalignment="top",
            bbox=props,
        )
        ax.grid(True, which="both", linestyle="--", linewidth=0.5)
        ax.set_xlabel("Date", fontsize=14)
        ax.set_ylabel("Cumulative Returns", fontsize=14)
        ax.set_title("Cumulative Returns Over Time", fontsize=16)
        plt.savefig(self.save_path + "Equity_curve.png")
        plt.show()

    def create_combcv_dict(self, n_splits: int, n_test_splits: int):
        """
        Create a dictionary for combinatorial cross-validation.

        Args:
            n_splits (int): Number of total splits.
            n_test_splits (int): Number of test splits.
        """

        def split_consecutive(arr):
            # Ensure the input is a NumPy array
            arr = np.asarray(arr)

            # Calculate the differences between adjacent elements
            diff = np.diff(arr)

            # Find where the difference is not 1 (i.e., where sequences break)
            split_points = np.where(diff != 1)[0] + 1

            # Use these split points to create chunks
            chunks = np.split(arr, split_points)

            return chunks

        total_comb = math.comb(n_splits, n_test_splits)
        if n_test_splits == 0 or n_splits == 0:
            logging.info(
                "Using the entire dataset as the training set with no validation groups."
            )
            self.combcv_dict[0] = {}
            for ticker, df in self.train_data.items():
                self.combcv_dict[0][ticker] = {
                    "train": [np.arange(len(df))],
                    "test": None,
                }
        else:
            logging.info(
                f"Creating combinatorial train-val split, total_split: {n_splits}, out of which val groups: {n_test_splits}"
            )
            for ticker, df in self.train_data.items():

                if len(df) > total_comb * 50:
                    data_length = len(df)
                    is_test, paths, path_folds = self.cpcv_generator(
                        data_length, n_splits, n_test_splits, verbose=False
                    )
                    self.backtest_paths[ticker] = paths
                    for combination_num in range(is_test.shape[1]):
                        if combination_num not in self.combcv_dict:
                            self.combcv_dict[combination_num] = {}

                        train_indices = np.where(~is_test[:, combination_num])[0]
                        test_indices = np.where(is_test[:, combination_num])[0]

                        train_indices = split_consecutive(train_indices)
                        test_indices = split_consecutive(test_indices)

                        self.combcv_dict[combination_num][ticker] = {
                            "train": train_indices,
                            "test": test_indices,
                        }

    def optimize(
        self,
        params: dict,
        n_jobs: int,
        n_runs: int,
        best_trials_pct: float,
        file_prefix: str = None,
    ):
        """
        Optimize parameters using Optuna.

        Args:
            params (dict): Initial parameters for the optimization.
            n_jobs (int): Number of parallel jobs.
            n_runs (int): Number of optimization runs.
            best_trials_pct (float): Percentage of best trials to consider.
            save_file_name (str, optional): File name to save the results.
        """
        result = []
        params_dict = params.copy()
        all_tested_params = []
        combcv_pl = partial(standalone_combcv_pl, self.calc_pl)

        def objective(trial: optuna.Trial, group_dict: dict) -> float:
            trial_params = {}
            for k, v in params_dict.items():
                if not isinstance(v, Iterable) or isinstance(v, (str, bytes)):
                    trial_params[k] = v
                elif all(isinstance(item, int) for item in v):
                    trial_params[k] = trial.suggest_categorical(k, v)
                elif any(isinstance(item, float) for item in v):
                    trial_params[k] = trial.suggest_categorical(k, v)
                else:
                    trial_params[k] = trial.suggest_categorical(k, v)

            current_params = trial.params
            existing_trials = trial.study.get_trials(deepcopy=False)
            completed_trials = [
                t
                for t in existing_trials
                if t.state == optuna.trial.TrialState.COMPLETE
            ]
            existing_params = [t.params for t in completed_trials]
            if current_params in existing_params:
                logging.info(
                    f"Pruning trial {trial.number} due to duplicate parameters: {trial_params}"
                )
                raise optuna.TrialPruned()

            sharpe, _ = combcv_pl(trial_params, group_dict)
            return sharpe

        # Create the objective function closure
        for fold_num, train_test_splits in self.combcv_dict.items():
            logging.info(f"Starting optimization for group: {fold_num}")
            self.current_group = train_test_splits
            train_group_dict = self.generate_group_dict(is_train=True)

            study = optuna.create_study(
                direction="maximize",
                sampler=optuna.samplers.TPESampler(multivariate=True),
            )
            study.optimize(
                lambda trial: objective(trial, train_group_dict),
                n_trials=n_runs,
                n_jobs=n_jobs,
            )

            all_trials = sorted(
                [
                    trial
                    for trial in study.trials
                    if trial.value is not None
                    and trial.state == optuna.trial.TrialState.COMPLETE
                ],
                key=lambda trial: trial.value,
                reverse=True,
            )
            all_trials = [trial.params for trial in all_trials]
            for trial_params in all_trials:
                for key, value in params_dict.items():
                    trial_params.setdefault(key, value)
            all_tested_params.extend(all_trials)
            top_params = all_trials[: max(1, int(len(all_trials) * best_trials_pct))]
            logging.info(f"Top {best_trials_pct} param combinations are: {top_params}")

            test_group_dict = self.generate_group_dict(is_train=False)
            for i, trial_params in enumerate(top_params):
                sharpe, returns_list = combcv_pl(trial_params, test_group_dict)
                if i == 0:
                    trial_params["fold_num"] = fold_num
                else:
                    trial_params["fold_num"] = np.nan
                trial_params["sharpe"] = sharpe
                result.append(trial_params)
                logging.info(f"Val performance: {trial_params}")

            logging.info(f"Best params: {result}")

            self.top_params_list = result
            self.all_tested_params = list(
                {frozenset(d.items()): d for d in all_tested_params}.values()
            )
            self.best_params_by_fold[fold_num] = top_params[0]

            if self.save_path is not None:
                param_df = pd.DataFrame(self.top_params_list).sort_values(
                    "sharpe", ascending=False
                )
                param_df.to_csv(
                    self.save_path + file_prefix + "top_params.csv", index=False
                )

                all_tested_params_df = pd.DataFrame(self.all_tested_params)
                all_tested_params_df.to_csv(
                    self.save_path + file_prefix + "all_tested_params.csv", index=False
                )
                logging.info(f"Interim optimization results saved to {self.save_path}")

    def load_best_params(self, file_name: str = None, params: dict = None):
        """
        Load the best parameters from a file or dictionary.

        Args:
            file_name (str, optional): File name to load parameters from.
            params (dict, optional): Dictionary of parameters.
        """
        if params is not None:
            self.top_params_list = params
        elif file_name is not None:
            self.top_params_list = pd.read_csv(file_name)

    def reconstruct_equity_curves(self):
        """
        Reconstruct equity curves based on the best parameters.
        """

        logging.info("Reconstructing val equity curves")

        arrays = list(self.backtest_paths.values())
        num_columns = arrays[0].shape[1]
        if not all(arr.shape[1] == num_columns for arr in arrays):
            raise Exception("Tickers have different number of backtest paths")

        for col in range(num_columns):
            unique_values_set = set(np.unique(arrays[0][:, col]))
            for arr in arrays[1:]:
                if unique_values_set != set(np.unique(arr[:, col])):
                    raise Exception(
                        "Tickers have different parameter folds within same backtest path number"
                    )

        n_paths = num_columns
        tmp_dict = {}
        final_metrics = []
        final_returns = []
        for path_num in tqdm(range(n_paths), desc="Path num"):
            logging.info(f"Starting for path {path_num}")
            path_returns = []
            unique_folds = np.unique(arrays[0][:, path_num])
            for fold in unique_folds:
                logging.info(f"Starting for fold {fold}")
                fold = int(fold)
                params = self.best_params_by_fold[fold]
                for ticker, path_array in self.backtest_paths.items():
                    test_indices = np.where(path_array[:, path_num] == fold)[0]
                    tmp_dict[ticker] = self.train_data[ticker].iloc[test_indices]
                returns = self.calc_pl(tmp_dict, params)
                path_returns.append(returns)

            path_returns = pd.concat(path_returns)
            final_returns.append(path_returns)
            metrics = calculate_metrics(path_returns)
            final_metrics.append(metrics)

        final_metrics = pd.DataFrame(final_metrics).mean().to_dict()

        text = ""
        for metric, v in final_metrics.items():
            text += f"Mean {metric}: {round(v, 2)}\n"

        fig, ax = plt.subplots(figsize=(12, 8))
        for returns in final_returns:
            ax.plot(returns.resample("D").sum().cumsum())

        props = dict(boxstyle="round", facecolor="wheat", alpha=0.5)
        ax.text(
            0.05,
            0.95,
            text,
            transform=ax.transAxes,
            fontsize=12,
            verticalalignment="top",
            bbox=props,
        )
        ax.grid(True, which="both", linestyle="--", linewidth=0.5)
        ax.set_xlabel("Date", fontsize=14)
        ax.set_ylabel("Cumulative Returns", fontsize=14)
        ax.set_title("Cumulative Returns Over Time", fontsize=16)
        plt.savefig(self.save_path + "CombCV_equity_curves.png")
        plt.show()

    def run_stress_tests(self, num_workers=5):
        """
        Run stress tests on the best parameter sets.
        """
        logging.info(f"Running stress tests, num_workers: {num_workers}")

        # Create local references to necessary data
        shared_train_data = self.train_data.copy()
        all_tested_params = self.all_tested_params.copy()
        calc_pl = self.calc_pl  # Local reference to avoid pickling self

        with Pool(processes=num_workers) as pool:
            # Create arguments for the calc_returns function
            args = [
                (calc_pl, params, shared_train_data) for params in all_tested_params
            ]
            results = list(
                tqdm(
                    pool.imap(calc_returns, args),
                    total=len(all_tested_params),
                    desc="Calculating individual returns",
                )
            )

        # Filter out empty DataFrames
        results = [r for r in results if not r.empty]

        if results:
            result_df = pd.concat(results, axis=1).dropna()
            run_stress_tests(result_df)

    def calculate_wcss(self, data: np.ndarray, max_clusters: int) -> list:
        """
        Calculate the within-cluster sum of squares (WCSS) for clustering.

        Args:
            data (np.ndarray): The data to be clustered.
            max_clusters (int): The maximum number of clusters.

        Returns:
            list: The WCSS for each number of clusters.
        """
        wcss = []
        for n_clusters in range(1, max_clusters + 1):
            clustering = AgglomerativeClustering(n_clusters=n_clusters)
            cluster_labels = clustering.fit_predict(data)
            centroids = [
                data[cluster_labels == i].mean(axis=0) for i in range(n_clusters)
            ]
            wcss.append(
                sum(
                    np.linalg.norm(data[cluster_labels == i] - centroids[i]) ** 2
                    for i in range(n_clusters)
                )
            )
        return wcss

    def find_optimal_clusters(
        self, param_matrix_scaled: np.ndarray, max_clusters: int
    ) -> int:
        """
        Find the optimal number of clusters using the elbow method.

        Args:
            param_matrix_scaled (np.ndarray): The scaled parameter matrix.
            max_clusters (int): The maximum number of clusters.

        Returns:
            int: The optimal number of clusters.
        """
        wcss = self.calculate_wcss(param_matrix_scaled, max_clusters)

        first_derivative = np.diff(wcss)
        second_derivative = np.diff(first_derivative)
        elbow_point = np.argmin(second_derivative) + 2  # +2 to correct the index offset

        plt.figure(figsize=(10, 7))
        plt.plot(range(1, max_clusters + 1), wcss, marker="o")
        plt.axvline(x=elbow_point, color="r", linestyle="--")
        plt.title("Elbow Method")
        plt.xlabel("Number of Clusters")
        plt.ylabel("WCSS")
        plt.show()

        return elbow_point

    def cluster_and_aggregate(self) -> dict:
        """
        Cluster parameter sets and aggregate the best parameters.

        Returns:
            dict: The aggregated best parameter set.
        """

        logging.info("Starting clustering")

        if isinstance(self.top_params_list, list):
            param_df = (
                pd.DataFrame(self.top_params_list)
                .drop(columns=["sharpe"])
                .dropna(axis=1)
            )
        elif isinstance(self.top_params_list, pd.DataFrame):
            param_df = self.top_params_list.drop(columns=["sharpe"]).dropna(axis=1)
            self.top_params_list = self.top_params_list.to_dict("records")
        else:
            raise Exception(
                "Wrong data format for top params, accepted formats are list/DataFrame"
            )

        max_clusters = min(max(3, len(param_df) // 3), len(param_df))
        if max_clusters > 2:
            logging.info(
                f"Starting clustering with max clusters: {max_clusters}, len of param set {len(param_df)}"
            )

            column_transformer = ColumnTransformer(
                transformers=[
                    (
                        "num",
                        StandardScaler(),
                        make_column_selector(dtype_include=np.number),
                    ),
                    (
                        "cat",
                        OneHotEncoder(),
                        make_column_selector(dtype_exclude=np.number),
                    ),
                ],
                remainder="passthrough",
            )

            param_matrix_scaled = column_transformer.fit_transform(param_df)
            best_n_clusters = (
                self.find_optimal_clusters(param_matrix_scaled, max_clusters)
                if max_clusters < len(param_df)
                else max_clusters
            )
            logging.info(f"Optimal number of clusters: {best_n_clusters}")
            clustering = AgglomerativeClustering(n_clusters=best_n_clusters)
            cluster_labels = clustering.fit_predict(param_matrix_scaled)

            clustered_params = {i: [] for i in range(best_n_clusters)}
            for param, cluster in zip(self.top_params_list, cluster_labels):
                clustered_params[cluster].append(param)

            best_cluster = max(
                clustered_params.keys(),
                key=lambda c: np.mean([p["sharpe"] for p in clustered_params[c]]),
            )
            best_cluster_params = clustered_params[best_cluster]
            logging.info(f"Best cluster: {best_cluster_params}")
            best_param_set = self.aggregate_params(best_cluster_params)
        else:
            logging.info(
                f"Len of params set less than 3, choosing best params out of 3"
            )
            best_param_set = pd.DataFrame(self.top_params_list).iloc[0].to_dict()
        logging.info(f"Best params: {best_param_set}")
        return best_param_set

    def param_to_vector(self, param_set: dict) -> list:
        """
        Convert a parameter set to a vector.

        Args:
            param_set (dict): The parameter set.

        Returns:
            list: The parameter vector.
        """
        vector = []
        for key in sorted(param_set.keys()):
            value = param_set[key]
            if isinstance(value, list):
                vector.extend(value)
            else:
                vector.append(value)
        return vector

    def aggregate_params(self, params_list: list) -> dict:
        """
        Aggregate parameters by computing the mean of numerical values and the most frequent value of categorical values.

        Args:
            params_list (list): List of parameter sets.

        Returns:
            dict: The aggregated parameter set.
        """
        aggregated = {}
        for key in params_list[0].keys():
            values = [param[key] for param in params_list]
            if isinstance(values[0], list):
                aggregated[key] = list(np.mean(values, axis=0))
            elif isinstance(values[0], bool):
                aggregated[key] = max(set(values), key=values.count)
            elif isinstance(values[0], (int, float, np.number)):
                aggregated[key] = np.mean(values)
            else:
                aggregated[key] = max(set(values), key=values.count)
        return aggregated

    def read_saved_params(self, file_prefix: str):

        logging.info("Loading saved params")

        top_params_df = pd.read_csv(self.save_path + file_prefix + "top_params.csv")
        self.top_params_list = top_params_df.drop(columns=["fold_num"])
        self.all_tested_params = pd.read_csv(
            self.save_path + file_prefix + "all_tested_params.csv"
        ).to_dict("records")

        top_params_list = top_params_df.dropna(subset="fold_num").to_dict("records")
        for tp in top_params_list:
            self.best_params_by_fold[tp["fold_num"]] = tp

        logging.info("Params loaded")

    def plot_multiple_param_combinations(
        self, data_dict: dict, params: dict, n_jobs: int
    ):
        """
        Calculate out-of-sample Sharpe ratio and plot cumulative returns for multiple parameter combinations.

        Args:
            data_dict: dictionary with data in format {'ticker':DF,...}
            params (dict): Parameters for the performance calculation. May contain lists of values.
            n_jobs: number of processes
        """

        logging.info("Plotting results for multiple parameter combinations")

        # Generate all combinations of parameters
        param_names = []
        param_values = []
        for key, value in params.items():
            if isinstance(value, list):
                param_names.append(key)
                param_values.append(value)
            else:
                param_names.append(key)
                param_values.append([value])

        param_combinations = list(itertools.product(*param_values))

        # Prepare the partial function for multiprocessing
        partial_process = partial(
            self._process_combination, data_dict=data_dict, param_names=param_names
        )

        # Use multiprocessing to calculate returns for all combinations
        with Pool(processes=n_jobs) as pool:
            results = pool.map(partial_process, param_combinations)

        # Plot and save results for each combination
        for i, (returns, metrics, param_set) in enumerate(results):
            self._plot_and_save(returns, metrics, param_set, i)

    def _process_combination(self, combination, data_dict, param_names):
        """Process a single parameter combination."""
        param_set = dict(zip(param_names, combination))
        returns = self.calc_pl(data_dict, param_set)
        metrics = calculate_metrics(returns)
        return returns, metrics, param_set

    def _plot_and_save(self, returns, metrics, param_set, index):
        """Plot and save results for a single parameter combination."""
        fig, (ax, text_ax) = plt.subplots(
            2, 1, figsize=(12, 10), gridspec_kw={"height_ratios": [3, 1]}
        )

        # Plot the cumulative returns
        ax.plot(returns.cumsum())
        ax.grid(True, which="both", linestyle="--", linewidth=0.5)
        ax.set_xlabel("Date", fontsize=14)
        ax.set_ylabel("Cumulative Returns", fontsize=14)
        ax.set_title(
            f"Cumulative Returns Over Time (Combination {index + 1})", fontsize=16
        )

        # Prepare text for metrics and parameters
        text = "Metrics:\n"
        for metric, v in metrics.items():
            text += f"{metric}: {round(v, 2)}, "
        text += "\nParameters:\n"
        for param, value in param_set.items():
            text += f"{param}: {value}, "

        # Display text below the chart
        text_ax.axis("off")
        text_ax.text(
            0.5,
            1.0,
            text,
            ha="center",
            va="top",
            fontsize=10,
            wrap=True,
            bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5),
        )

        # Adjust layout and save the figure
        plt.tight_layout()
        plt.savefig(f"{self.save_path}Equity_curve_combination_{index + 1}.png")
        plt.close(fig)
