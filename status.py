"""Affiche l'état du bot sans le lancer."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from monitoring.pnl_tracker import print_open_positions, print_pnl_summary
from signals.sources.model_calibrator import print_bias_report

print_open_positions()
print_pnl_summary()
print_bias_report()