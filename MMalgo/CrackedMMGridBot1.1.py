import json
import time
import logging
import os
from typing import List, Dict, Optional, Literal
from decimal import Decimal, ROUND_DOWN

import eth_account
from eth_account.signers.local import LocalAccount

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('gridbot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Type for order side
OrderSide = Literal["buy", "sell"]

def setup(base_url=None, skip_ws=False):
    """Setup connection to Hyperliquid"""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path) as f:
        config = json.load(f)
    account: LocalAccount = eth_account.Account.from_key(config["secret_key"])
    address = config["account_address"].lower()
    if address == "":
        address = account.address.lower()
    print("Running with account address:", address)
    if address != account.address.lower():
        print("Running with agent address:", account.address.lower())
    info = Info(base_url, skip_ws)
    exchange = Exchange(account, base_url, account_address=address)
    return address, info, exchange

class GridBot:
    def __init__(self, address: str, info: Info, exchange: Exchange):
        """Initialize the grid bot with necessary connections"""
        self.address = address
        self.info = info
        self.exchange = exchange
        self.active_orders: List[Dict] = []
        self.meta = self.info.meta()
        self.sz_decimals = {
            asset["name"]: asset["szDecimals"] 
            for asset in self.meta["universe"]
        }

    def set_leverage(self, asset: str, leverage: int = 1) -> None:
        """Set leverage for the specified asset"""
        try:
            logger.info(f"Setting leverage for {asset} to {leverage}x")
            result = self.exchange.update_leverage(leverage, asset)
            if result["status"] == "ok":
                logger.info(f"Successfully set leverage to {leverage}x for {asset}")
            else:
                logger.error(f"Failed to set leverage: {result}")
        except Exception as e:
            logger.error(f"Error setting leverage: {str(e)}")
            raise

    def get_position_info(self, asset: str) -> Optional[Dict]:
        """
        Get current position information.
        Returns the position dictionary (containing keys like 'szi', 'entryPx', etc.)
        if an open position exists; otherwise, returns None.
        """
        try:
            user_state = self.info.user_state(self.address)
            for position in user_state["assetPositions"]:
                if position["position"]["coin"] == asset:
                    logger.info(f"Current position for {asset}:")
                    logger.info(f"Size: {position['position']['szi']}")
                    logger.info(f"Leverage: {position['position']['leverage']}")
                    logger.info(f"Entry Price: {position['position']['entryPx']}")
                    return position["position"]
            logger.info(f"No existing position found for {asset}")
            return None
        except Exception as e:
            logger.error(f"Error getting position info: {str(e)}")
            raise

    def round_size(self, size: float, asset: str) -> float:
        """Round size according to asset's size decimals"""
        decimals = self.sz_decimals.get(asset, 6)
        return float(Decimal(str(size)).quantize(Decimal('0.' + '0' * decimals), rounding=ROUND_DOWN))

    def round_price(self, price: float, asset: Optional[str] = None) -> float:
        """
        Round price according to asset's requirements.
        Prices can have up to 5 significant figures, but no more than MAX_DECIMALS - szDecimals decimal places
        where MAX_DECIMALS is 6 for perps.
        """
        if price > 100_000:
            # For large prices, round to integer
            return float(Decimal(str(price)).quantize(Decimal('0'), rounding=ROUND_DOWN))
        
        # Get max allowed decimals based on asset's szDecimals
        max_decimals = 6  # MAX_DECIMALS for perps
        if asset:
            sz_decimals = self.sz_decimals.get(asset, 0)
            allowed_decimals = max_decimals - sz_decimals
        else:
            allowed_decimals = max_decimals

        # First round to 5 significant figures
        price = float(f"{price:.5g}")
        
        # Then ensure we don't exceed allowed decimal places
        return float(Decimal(str(price)).quantize(Decimal('0.' + '0' * allowed_decimals), rounding=ROUND_DOWN))

    def get_current_price(self, asset: str) -> float:
        """Get current mid price for the asset"""
        mids = self.info.all_mids()
        if asset not in mids:
            raise ValueError(f"Asset {asset} not found in available markets")
        return float(mids[asset])

    def cancel_all_orders(self, asset: str) -> None:
        """Cancel all existing orders for the specified asset"""
        try:
            open_orders = self.info.open_orders(self.address)
            cancelled_count = 0
            for order in open_orders:
                if order["coin"] == asset:
                    try:
                        cancel_result = self.exchange.cancel(asset, order["oid"])
                        if cancel_result["status"] == "ok":
                            cancelled_count += 1
                        time.sleep(0.1)
                    except Exception as e:
                        logger.error(f"Error cancelling order {order['oid']}: {str(e)}")
            logger.info(f"Cancelled {cancelled_count} orders for {asset}")
        except Exception as e:
            logger.error(f"Error in cancel_all_orders: {str(e)}")
            raise

    def calculate_grid_prices(self, current_price: float, num_orders: int, spacing_percentage: float, side: OrderSide, asset: str, spacing_multiplier: float = 1.0) -> List[float]:
        """Calculate grid prices with proper numerical handling and progressive spacing"""
        grid_prices = []
        base_spacing = spacing_percentage / 100.0
        cumulative_spacing = 0
        
        for i in range(num_orders):
            # Calculate progressive spacing by applying the multiplier for each step
            current_spacing = base_spacing * (spacing_multiplier ** i)
            cumulative_spacing += current_spacing
            
            multiplier = (1 - cumulative_spacing) if side == "buy" else (1 + cumulative_spacing)
            price = current_price * multiplier
            grid_prices.append(self.round_price(price, asset))
        return sorted(grid_prices, reverse=(side == "sell"))

    def calculate_progressive_sizes(self, base_size: float, num_orders: int, size_multiplier: float, asset: str) -> List[float]:
        """Calculate progressively increasing position sizes"""
        sizes = []
        for i in range(num_orders):
            size = base_size * (size_multiplier ** i)
            sizes.append(self.round_size(size, asset))
        return sizes

    def place_grid_orders(self, asset: str, side: OrderSide, position_size: float, num_orders: int, 
                         spacing_percentage: float, spacing_multiplier: float = 1.0, 
                         size_multiplier: float = 1.0) -> None:
        """Place grid orders with progressive spacing and sizes"""
        try:
            current_price = self.get_current_price(asset)
            grid_prices = self.calculate_grid_prices(
                current_price, num_orders, spacing_percentage, side, asset, spacing_multiplier
            )
            grid_sizes = self.calculate_progressive_sizes(position_size, num_orders, size_multiplier, asset)
            
            successful_orders = 0
            for price, size in zip(grid_prices, grid_sizes):
                try:
                    logger.debug(f"Placing {side} order: size={size:.5f}, price={price:.2f}")
                    order_result = self.exchange.order(
                        asset,
                        side == "buy",
                        size,
                        float(price),
                        {"limit": {"tif": "Gtc"}}
                    )
                    
                    if order_result["status"] == "ok":
                        successful_orders += 1
                        logger.info(f"Success: {side} order at {price:.2f} with size {size:.5f}")
                    else:
                        error = order_result["response"]["data"]["statuses"][0].get("error", "Unknown error")
                        logger.error(f"Order failed: {error}")
                    
                    time.sleep(0.1)
                except Exception as e:
                    logger.error(f"Order placement error: {str(e)}")
            
            logger.info(f"Placed {successful_orders}/{num_orders} orders")
        except Exception as e:
            logger.error(f"Grid order error: {str(e)}")
            raise

    def place_take_profit_order(self, asset: str, markup_percentage: float = 0.15) -> None:
        """
        Create a limit sell reduce-only order based on the current open position.
        The order is placed at a price 'markup_percentage' above the open position's entry price.
        """
        try:
            position_info = self.get_position_info(asset)
            if not position_info:
                logger.error("No open position found. Cannot place take profit order.")
                return
            
            # Extract entry price and current position size
            entry_price = float(position_info["entryPx"])
            position_size = float(position_info["szi"])
            
            # Calculate target price markup_percentage above the entry price.
            target_price = entry_price * (1 + markup_percentage / 100)
            target_price = self.round_price(target_price, asset)
            
            logger.info(f"Placing take profit order: Sell {position_size:.5f} {asset} at {target_price:.2f} (reduce only)")
            order_result = self.exchange.order(
                asset,
                False,  # False indicates a sell order
                self.round_size(position_size, asset),
                target_price,
                {"limit": {"tif": "Gtc"}, "reduceOnly": True}
            )
            
            if order_result["status"] == "ok":
                logger.info(f"Successfully placed take profit order at {target_price:.2f}")
            else:
                error = order_result["response"]["data"]["statuses"][0].get("error", "Unknown error")
                logger.error(f"Take profit order failed: {error}")
        except Exception as e:
            logger.error(f"Error placing take profit order: {str(e)}")
            raise

def main():
    address, info, exchange = setup(base_url=constants.TESTNET_API_URL, skip_ws=True)
    bot = GridBot(address, info, exchange)
    
    # Trading parameters
    params = {
        "asset": "BTC",
        "side": "buy",
        "position_size": 0.002,
        "num_orders": 4,
        "spacing_percentage": 0.5,
        "spacing_multiplier": 1.5,  # Each level's spacing will be 1.5x the previous
        "size_multiplier": 0.8,     # Each level's size will be 1.2x the previous
        "leverage": 20
    }
    
    logger.info(f"Starting grid bot for {params['asset']}")
    
    while True:
        try:
            # (Optional) Re-set leverage each cycle if needed
            bot.set_leverage(params["asset"], params["leverage"])
            
            # Check the current position
            bot.get_position_info(params["asset"])
            
            # Cancel any open orders for the asset
            bot.cancel_all_orders(params["asset"])
            
            # Remove the "leverage" key from params when calling place_grid_orders
            grid_order_params = {k: v for k, v in params.items() if k != "leverage"}
            bot.place_grid_orders(**grid_order_params)
            
            # Place the take profit order after setting up grid orders
            bot.place_take_profit_order(params["asset"], markup_percentage=0.15)
            
            logger.info("Grid setup and take profit order placement completed successfully")
        except Exception as e:
            logger.error(f"Error during trading cycle: {str(e)}")
        
        # Wait for one hour (3600 seconds) before the next cycle
        logger.info("Sleeping for one hour before the next cycle...")
        time.sleep(3600)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Exiting gracefully...")
