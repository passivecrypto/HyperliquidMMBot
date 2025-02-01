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

# Configure basic logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
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
    print("\nMMALGO 1.0 Hyperliquid - Initializing...")
    print("Created by Starbringer Trading")
    print("Github: PassiveCrypto\n")
    
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
            result = self.exchange.update_leverage(leverage, asset)
            if result["status"] != "ok":
                logger.error(f"Failed to set leverage: {result}")
        except Exception as e:
            logger.error(f"Error setting leverage: {str(e)}")
            raise

    def get_position_info(self, asset: str) -> Optional[Dict]:
        """
        Get current position information.
        Returns the position dictionary if an open position exists; otherwise, returns None.
        """
        try:
            user_state = self.info.user_state(self.address)
            for position in user_state["assetPositions"]:
                if position["position"]["coin"] == asset:
                    return position["position"]
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
        except Exception as e:
            logger.error(f"Error in cancel_all_orders: {str(e)}")
            raise

    def calculate_grid_prices(self, current_price: float, num_orders: int, spacing_percentage: float, side: OrderSide, asset: str) -> List[float]:
        """Calculate grid prices with proper numerical handling"""
        grid_prices = []
        spacing_factor = spacing_percentage / 100.0
        for i in range(num_orders):
            multiplier = (1 - spacing_factor * (i + 1)) if side == "buy" else (1 + spacing_factor * (i + 1))
            price = current_price * multiplier
            grid_prices.append(self.round_price(price, asset))
        return sorted(grid_prices, reverse=(side == "sell"))

    def place_grid_orders(self, asset: str, side: OrderSide, position_size: float, num_orders: int, spacing_percentage: float) -> None:
        """Place grid orders with corrected numerical handling"""
        try:
            current_price = self.get_current_price(asset)
            grid_prices = self.calculate_grid_prices(current_price, num_orders, spacing_percentage, side, asset)
            size_per_order = self.round_size(position_size, asset)
            
            successful_orders = 0
            for price in grid_prices:
                try:
                    order_result = self.exchange.order(
                        asset,
                        side == "buy",
                        size_per_order,
                        float(price),
                        {"limit": {"tif": "Gtc"}}
                    )
                    
                    if order_result["status"] == "ok":
                        successful_orders += 1
                    else:
                        error = order_result["response"]["data"]["statuses"][0].get("error", "Unknown error")
                        logger.error(f"Order failed: {error}")
                    
                    time.sleep(0.1)
                except Exception as e:
                    logger.error(f"Order placement error: {str(e)}")
            
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
                return
            
            entry_price = float(position_info["entryPx"])
            position_size = float(position_info["szi"])
            
            target_price = entry_price * (1 + markup_percentage / 100)
            target_price = self.round_price(target_price, asset)
            
            order_result = self.exchange.order(
                asset,
                False,
                self.round_size(position_size, asset),
                target_price,
                {"limit": {"tif": "Gtc"}, "reduceOnly": True}
            )
            
            if order_result["status"] != "ok":
                error = order_result["response"]["data"]["statuses"][0].get("error", "Unknown error")
                logger.error(f"Take profit order failed: {error}")
        except Exception as e:
            logger.error(f"Error placing take profit order: {str(e)}")
            raise

def main():
    address, info, exchange = setup(base_url=constants.MAINNET_API_URL, skip_ws=True)
    bot = GridBot(address, info, exchange)
    
    params = {
        "asset": "BTC",
        "side": "buy",
        "position_size": 0.001,
        "num_orders": 4,
        "spacing_percentage": 0.5,
        "leverage": 20
    }
    
    while True:
        try:
            bot.set_leverage(params["asset"], params["leverage"])
            bot.get_position_info(params["asset"])
            bot.cancel_all_orders(params["asset"])
            
            grid_order_params = {k: v for k, v in params.items() if k != "leverage"}
            bot.place_grid_orders(**grid_order_params)
            bot.place_take_profit_order(params["asset"], markup_percentage=0.15)
            
        except Exception as e:
            logger.error(f"Error during trading cycle: {str(e)}")
        
        time.sleep(3600)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting MMALGO 1.0...")
