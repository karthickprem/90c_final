"""
Auto-redemption module for Polymarket CTF positions
Implements on-chain claim via redeemPositions
"""

import json
from web3 import Web3
from eth_account import Account

# Polygon RPC
RPC_URL = "https://polygon-rpc.com"

# Contract addresses on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # ConditionalTokens on Polygon

# CTF ABI (minimal - just what we need)
CTF_ABI = [
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

class PolymarketRedeemer:
    def __init__(self, private_key: str, proxy_address: str):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.private_key = private_key
        self.proxy = Web3.to_checksum_address(proxy_address)
        self.account = Account.from_key(private_key)
        
        # CTF contract
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI
        )
        
        self.usdc = Web3.to_checksum_address(USDC_ADDRESS)
    
    def is_resolved_onchain(self, condition_id: str) -> bool:
        """Check if market is resolved on-chain (not API)"""
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            payout_denom = self.ctf.functions.payoutDenominator(condition_bytes).call()
            return payout_denom > 0
        except Exception as e:
            print(f"Error checking resolution: {e}")
            return False
    
    def redeem_position(self, condition_id: str, gas_price_gwei: int = 50) -> dict:
        """
        Redeem a resolved position
        
        Args:
            condition_id: hex string like "0x4a629eb..."
            gas_price_gwei: gas price in gwei
        
        Returns:
            dict with tx_hash and success status
        """
        try:
            # Check if resolved
            if not self.is_resolved_onchain(condition_id):
                return {"success": False, "error": "Not resolved on-chain yet"}
            
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)  # Zero bytes32
            index_sets = [1, 2]  # Binary market: [YES, NO]
            
            # Build transaction
            nonce = self.w3.eth.get_transaction_count(self.account.address)
            
            tx = self.ctf.functions.redeemPositions(
                self.usdc,
                parent_collection,
                condition_bytes,
                index_sets
            ).build_transaction({
                'from': self.account.address,
                'nonce': nonce,
                'gas': 300000,  # Estimate
                'gasPrice': self.w3.to_wei(gas_price_gwei, 'gwei'),
                'chainId': 137  # Polygon
            })
            
            # Sign and send
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            print(f"Redemption tx sent: {tx_hash.hex()}")
            
            # Wait for receipt
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            success = (receipt['status'] == 1)
            
            return {
                "success": success,
                "tx_hash": tx_hash.hex(),
                "gas_used": receipt['gasUsed']
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}


def load_config():
    with open("pm_api_config.json") as f:
        return json.load(f)


if __name__ == "__main__":
    """Test redemption"""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python pm_redeem.py <conditionId>")
        print("Example: python pm_redeem.py 0x4a629eb456c10ea56e4819f5b54c6727be8010b03c31375fed5b9f100f0dee53")
        sys.exit(1)
    
    condition_id = sys.argv[1]
    
    config = load_config()
    redeemer = PolymarketRedeemer(
        config["private_key"],
        config["proxy_address"]
    )
    
    print(f"Checking resolution for: {condition_id}")
    resolved = redeemer.is_resolved_onchain(condition_id)
    print(f"Resolved on-chain: {resolved}")
    
    if resolved:
        print("\nAttempting redemption...")
        result = redeemer.redeem_position(condition_id)
        print(f"Result: {result}")
    else:
        print("Market not resolved yet - cannot redeem")

