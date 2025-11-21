import os
import logging
from dotenv import load_dotenv

from lib import ai, custom_helpers, ForwardTester, BitunixFutures, BitunixError, DiscordNotifier

load_dotenv()

# ===================== CONFIGURATION =====================
RUN_NAME = "run_btc_template_prompt"
CRYPTO = "Bitcoin"
SYMBOL = "BTCUSDT"
LEVERAGE = 1
MARGIN_MODE = "ISOLATION"

# Position Size Configuration
# POSITION_SIZE = "10%"  # Use 10% of capital per trade
POSITION_SIZE = 20  # Use 20 USDT per trade

# Stop Loss Configuration (LIVE TRADING ONLY - not supported in forward testing yet)
STOP_LOSS_PERCENT = 10  # 10% stop-loss from entry price
# STOP_LOSS_PERCENT = None  # Disabled

# Forward Testing Configuration
FORWARD_TESTING_CONFIG = None
# FORWARD_TESTING_CONFIG = {
#     "run_name": RUN_NAME,
#     "initial_capital": 10000,
#     "fees": 0.0006,  # 0.06% taker fee
# }

# Discord Notification Configuration
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")  # Get from .env or empty string
DISCORD_INCLUDE_REASON = True  # Set to False to exclude AI reasoning from notifications

PROMPT = f"""
You are a cryptocurrency market analyst AI.

Based on your general knowledge of cryptocurrency markets and typical {CRYPTO} behavior patterns,
provide a trading outlook for the next 24 hours: Bullish, Bearish, or Neutral.

Consider general market factors:
- Common technical patterns and market cycles
- Typical support/resistance behavior
- General sentiment trends in crypto markets
- Broader macro conditions affecting crypto

Provide your reasoning in ~100-150 words, focusing on the most relevant general factors.

Return the result by calling the provided function/tool with your outlook and reasoning.
""".strip()

# ===================== PREP =====================
LLM_API_KEY = os.environ.get("LLM_API_KEY")
EXCHANGE_API_KEY = os.environ.get("EXCHANGE_API_KEY")
EXCHANGE_API_SECRET = os.environ.get("EXCHANGE_API_SECRET")

# ===================== MAIN EXECUTION =====================
custom_helpers.configure_logger(RUN_NAME)
logging.info("=== Run Started ===")

# Initialize exchange client (real or forward testing)
if FORWARD_TESTING_CONFIG is not None:
    exchange = ForwardTester(FORWARD_TESTING_CONFIG)
    logging.info("Forward testing mode enabled")
else:
    exchange = BitunixFutures(EXCHANGE_API_KEY, EXCHANGE_API_SECRET)
    logging.info("Live trading mode enabled")

# Initialize Discord notifier if webhook URL is provided
discord_notifier = None
if DISCORD_WEBHOOK_URL:
    try:
        discord_notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
        logging.info("Discord notifications enabled")
    except ValueError as e:
        logging.warning(f"Discord notifier initialization failed: {e}")

#  Call AI to get interpretation
try:
    outlook = ai.send_request(PROMPT, CRYPTO, LLM_API_KEY)
    interpretation = outlook.interpretation
    logging.info(f"AI Interpretation: {interpretation}")
except (ai.AIResponseError, Exception) as e:
    logging.warning(f"AI request failed, defaulting to Neutral: {e}")
    interpretation = "Neutral"
    outlook = None

if outlook:
    ai.save_response(outlook, RUN_NAME)

# Send Discord notification if enabled
if discord_notifier and outlook:
    try:
        discord_notifier.send_notification(
            run_name=RUN_NAME,
            interpretation=outlook.interpretation,
            reason=outlook.reasons,
            include_reason=DISCORD_INCLUDE_REASON
        )
    except Exception as e:
        logging.warning(f"Discord notification failed: {e}")

# Call exchange to get current position status
try:
    position = exchange.get_pending_positions(symbol=SYMBOL)
    current_position = position.side.lower() if position else None
    logging.info(f"Current Position: {current_position}")
    logging.info(f"Available Capital: {exchange.get_account_balance('USDT')} USDT")

    # Execute trading actions
    exchange.set_margin_mode(SYMBOL, MARGIN_MODE)
    exchange.set_leverage(SYMBOL, LEVERAGE)

    match (interpretation, current_position):
        # Bullish cases
        case ("Bullish", None):
            logging.info("Bullish signal: Opening long position")
            custom_helpers.open_position(exchange, SYMBOL, direction="buy",
                                        position_size=POSITION_SIZE, stop_loss_percent=STOP_LOSS_PERCENT)

        case ("Bullish", "sell"):
            logging.info("Bullish signal: Closing short, opening long")
            exchange.flash_close_position(position.positionId)
            custom_helpers.open_position(exchange, SYMBOL, direction="buy",
                                        position_size=POSITION_SIZE, stop_loss_percent=STOP_LOSS_PERCENT)

        case ("Bullish", "buy"):
            logging.info("Bullish signal: Already in long position, holding")

        # Bearish cases
        case ("Bearish", None):
            logging.info("Bearish signal: Opening short position")
            custom_helpers.open_position(exchange, SYMBOL, direction="sell",
                                        position_size=POSITION_SIZE, stop_loss_percent=STOP_LOSS_PERCENT)

        case ("Bearish", "buy"):
            logging.info("Bearish signal: Closing long, opening short")
            exchange.flash_close_position(position.positionId)
            custom_helpers.open_position(exchange, SYMBOL, direction="sell",
                                        position_size=POSITION_SIZE, stop_loss_percent=STOP_LOSS_PERCENT)

        case ("Bearish", "sell"):
            logging.info("Bearish signal: Already in short position, holding")

        # Neutral cases
        case ("Neutral", "buy" | "sell"):
            logging.info(f"Neutral signal: Closing {current_position} position")
            exchange.flash_close_position(position.positionId)
        case ("Neutral", None):
            logging.info("Neutral signal: No position open, doing nothing")

    logging.info("=== Run Completed ===")

except (BitunixError, Exception) as e:
    logging.warning(f"Exchange operation failed, stopping execution: {e}")

    # SAFETY: Flash close any open position on error
    try:
        position = exchange.get_pending_positions(symbol=SYMBOL)
        if position:
            logging.warning("Emergency flash close triggered due to error")
            exchange.flash_close_position(position.positionId)
            logging.info("Emergency flash close completed")
    except Exception as close_error:
        logging.error(f"Failed to flash close position: {close_error}")

    logging.info("=== Run Failed ===")
