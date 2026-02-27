"""Polyclaw — AI-powered news edge scanner and auto-trader for Polymarket."""

__version__ = "1.1.0"
__author__ = "arkYu"

from .edge_calculator import TradeSignal, find_edges
from .probability_engine import ProbEstimate, compute_estimates
from .news_ingestion import ingest
from .market_cache import get_markets
from .event_parser import parse_all, parse_with_llm
from .llm_analyzer import LLMSignal, analyze_news_batch
from .position_manager import open_position, check_exits, display_positions
from .scanner import run_scan, main
