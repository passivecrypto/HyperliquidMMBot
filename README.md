# HyperliquidMMBot
An Industry Standard Bot
Hyperliquid API trading with Python.

This bot will take orders and sell them at a profit for 20 basis points and repeat until you stop the bot.

</div>

## Installation
Create Virtual Environment

Install Requirements. 

Generate a new API key for an API Wallet
Generate and authorize a new API private key on https://app.hyperliquid.xyz/API, and set the API wallet's private key as the `secret_key` in config.json. Note that you must still set the public key of the main wallet *not* the API wallet as the `account_address` in config.json
## Configuration 

Set the public key as the `account_address` in config.json.
 
Set your private key as the `secret_key` in config.json.
 
Set your desired settings in parameters
 
## Run the Bot


## Updates
2025-02-06 CrackedGridBot1.1 Has been released.

New features:

- Improved Trade side order functionality
: Size multiplier Each level's size will be x the previous
: Spacing multiplier Each level's spacing will be x the previous

These features allow for improved risk management during increased market dislocation events. The Gridbot manages the drawdown better by utilizing geometric arrays in sizing and spacing.
Set parameters to 1 for arithmetic sequencing.

## New features coming soon

-Grid for take profit structure
-Initial offset percentage to enable order block stalking.
-Dynamic and improved position management.
and more!

Stop loss feature is available soon but I recommend risk to be your account balance.


