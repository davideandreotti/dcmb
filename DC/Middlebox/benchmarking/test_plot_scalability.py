"""Regression checks for fixed-load selection and historical memory isolation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from matplotlib.axes import Axes

import plot_latency as plot


DEPLOYMENTS = (
    "baremetal_full_reuse", "sgx_full_reuse",
    "docker_full_reuse", "docker_sgxgo_full_reuse",
)


def rows_for(rates=(0, 10, 500), clients=(1, 10, 30, 40)):
    return [
        {
            "deployment": deployment, "mode": "persistent", "clients": str(count),
            "offered_rps": str(rate), "point_quality": "valid",
            "achieved_rps": "10000", "mean_latency_ms": str(rate / 100),
            "mean_cpu_percent_p99_filtered": "1", "p99_memory_mib": str(rate),
        }
        for deployment in DEPLOYMENTS for count in clients for rate in rates
    ]


class ScalabilityFilteringTests(unittest.TestCase):
    def check_selected_rate(self, rate, rates=(0, 10, 500)):
        recorded = []
        original = Axes.errorbar

        def record(ax, x, y, *args, **options):
            recorded.append(list(y))
            return original(ax, x, y, *args, **options)

        with tempfile.TemporaryDirectory(prefix="dcmb-plot-test-") as directory:
            with patch.object(Axes, "errorbar", autospec=True, side_effect=record):
                paths = plot.plot_aggregate_scalability(
                    rows_for(rates), Path(directory) / "P7.pdf",
                )
            self.assertEqual(len(paths), 3)
            self.assertTrue(all(path.stat().st_size > 0 for path in paths))
        latency_series = [series for series in recorded if series[0] != 10000]
        self.assertEqual(len(latency_series), 8)
        self.assertTrue(all(all(value == rate / 100 for value in series) for series in latency_series))

    def test_default_selects_500_not_historical_10(self):
        self.check_selected_rate(500)

    def test_historical_rate_selected_when_500_absent(self):
        self.check_selected_rate(10, rates=(0, 10))

    def test_partial_500_does_not_mix_in_historical_10(self):
        rows = rows_for((0, 10)) + rows_for((500,))[:1]
        with tempfile.TemporaryDirectory(prefix="dcmb-plot-test-") as directory:
            output = Path(directory) / "P7.pdf"
            self.assertEqual(plot.plot_aggregate_scalability(rows, output), [])
            self.assertFalse(output.exists())

    def test_partial_shared_rerun_needs_retained_container_closed_loop(self):
        rows = [row for row in rows_for((0, 500)) if not (
            row["deployment"].startswith("docker") and row["offered_rps"] == "0"
        )]
        with tempfile.TemporaryDirectory(prefix="dcmb-plot-test-") as directory:
            self.assertEqual(plot.plot_aggregate_scalability(rows, Path(directory) / "P7.pdf"), [])

    def test_historical_memory_table_does_not_use_500_rps(self):
        with tempfile.TemporaryDirectory(prefix="dcmb-plot-test-") as directory:
            output = Path(directory) / "T2.tex"
            self.assertTrue(plot.write_clients_memory_latex(rows_for(), output))
            text = output.read_text()
            self.assertIn("10.00", text)
            self.assertNotIn("500.00", text)
            self.assertFalse(plot.write_clients_memory_latex(rows_for((500,)), Path(directory) / "new.tex"))


class PlatformComparisonTests(unittest.TestCase):
    def check_overlay(self, platforms, expected_overlay):
        rows = rows_for((0, 500))
        if platforms == ("SGX2",):
            rows = [
                dict(row, deployment="docker_sgxgo_full_reuse_sgx2")
                if row["deployment"] == "docker_sgxgo_full_reuse" else row
                for row in rows
            ]
        elif len(platforms) == 2:
            rows += [
                dict(row, deployment="docker_sgxgo_full_reuse_sgx2")
                for row in rows if row["deployment"] == "docker_sgxgo_full_reuse"
            ]
        styles = []
        original = Axes.errorbar

        def record(ax, *args, **kwargs):
            styles.append(kwargs.get("linestyle"))
            return original(ax, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="dcmb-platform-test-") as directory:
            with patch.object(Axes, "errorbar", autospec=True, side_effect=record):
                self.assertEqual(len(plot.plot_aggregate_scalability(rows, Path(directory) / "P7.pdf")), 3)
        self.assertEqual("--" in styles, expected_overlay)

    def test_sgx1_only_uses_standard_plot(self):
        self.check_overlay(("SGX1",), False)

    def test_sgx2_only_uses_standard_plot(self):
        self.check_overlay(("SGX2",), False)

    def test_two_platforms_enable_overlay(self):
        self.check_overlay(("SGX1", "SGX2"), True)

    def test_epc_comparison_requires_both_platforms_with_matching_workload(self):
        rows = [
            {"run_dir": "docker_sgxgo_clients1", "clients": 1, "rate": 0},
            {"run_dir": "docker_sgx2_clients1", "clients": 1, "rate": 0},
        ]
        with tempfile.TemporaryDirectory(prefix="dcmb-epc-test-") as directory:
            with patch.object(plot, "epc_run_metrics", return_value=(100, 25, 100000000)):
                self.assertEqual(plot.plot_epc_scalability(rows[:1], Path(directory)), [])
                self.assertEqual(plot.plot_epc_scalability(rows[1:], Path(directory)), [])
                unmatched = [rows[0], dict(rows[1], rate=10)]
                self.assertEqual(plot.plot_epc_scalability(unmatched, Path(directory)), [])
                paths = plot.plot_epc_scalability(rows, Path(directory))
                self.assertEqual({path.name for path in paths}, {
                    "P7c-client-scalability-epc-closed.pdf",
                    "P7e-client-scalability-epc-occupancy-closed.pdf",
                })
                self.assertTrue(all(path.stat().st_size > 0 for path in paths))


if __name__ == "__main__":
    unittest.main()
