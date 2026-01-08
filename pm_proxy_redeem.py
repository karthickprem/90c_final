"""
Correct auto-redemption via Polymarket Proxy Wallet

Key insight: Positions (ERC1155) are held by PROXY, not EOA.
Redemption must be executed FROM the proxy wallet.
"""

import json
from web3 import Web3
from eth_account import Account

# Polygon RPC
RPC_URL = "https://polygon-rpc.com"

# Contract addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Polymarket Proxy Wallet minimal ABI
# This is the execute function that lets EOA tell proxy to call another contract
PROXY_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"}
        ],
        "name": "execute",
        "outputs": [{"name": "", "type": "bytes"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

# CTF ABI
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


class ProxyRedeemer:
    def __init__(self, private_key: str, proxy_address: str):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.private_key = private_key
        self.proxy_addr = Web3.to_checksum_address(proxy_address)
        self.account = Account.from_key(private_key)
        
        # Proxy wallet contract
        self.proxy = self.w3.eth.contract(
            address=self.proxy_addr,
            abi=PROXY_ABI
        )
        
        # CTF contract (for building calldata and checking resolution)
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI
        )
        
        self.usdc = Web3.to_checksum_address(USDC_ADDRESS)
    
    def is_resolved_onchain(self, condition_id: str) -> bool:
        """Check if market is resolved on-chain"""
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            payout_denom = self.ctf.functions.payoutDenominator(condition_bytes).call()
            return payout_denom > 0
        except Exception as e:
            print(f"Error checking resolution: {e}")
            return False
    
    def redeem_via_proxy(self, condition_id: str, gas_price_gwei: int = 50) -> dict:
        """
        Redeem position by executing redeemPositions THROUGH the proxy wallet
        
        Flow:
        1. Build calldata for CTF.redeemPositions(...)
        2. Send tx to Proxy.execute(CTF_address, 0, calldata)
        3. Proxy calls CTF on behalf of itself
        4. USDC payout goes to proxy wallet
        """
        try:
            # Check if resolved
            if not self.is_resolved_onchain(condition_id):
                return {"success": False, "error": "Not resolved on-chain yet"}
            
            print("Building redemption calldata...")
            
            # Step 1: Build calldata for CTF.redeemPositions
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
            index_sets = [1, 2]  # Binary market
            
            redeem_calldata = self.ctf.encodeABI(
                fn_name="redeemPositions",
                args=[
                    self.usdc,
                    parent_collection,
                    condition_bytes,
                    index_sets
                ]
            )
            
            print(f"Calldata: {redeem_calldata[:66]}...")
            
            # Step 2: Build transaction to Proxy.execute(CTF, 0, calldata)
            # This tells proxy: "call CTF contract with this data"
            nonce = self.w3.eth.get_transaction_count(self.account.address)
            
            tx = self.proxy.functions.execute(
                Web3.to_checksum_address(CTF_ADDRESS),  # target: CTF contract
                0,  # value: 0 ETH
                redeem_calldata  # data: the redeemPositions call
            ).build_transaction({
                'from': self.account.address,  # EOA signs
                'nonce': nonce,
                'gas': 400000,  # Higher gas for proxy execution
                'gasPrice': self.w3.to_wei(gas_price_gwei, 'gwei'),
                'chainId': 137
            })
            
            print("Signing and sending tx...")
            
            # Step 3: Sign with EOA and send
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            print(f"Tx sent: {tx_hash.hex()}")
            print("Waiting for confirmation...")
            
            # Wait for receipt
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            success = (receipt['status'] == 1)
            
            if success:
                print(f"✓ Redemption successful! Gas used: {receipt['gasUsed']}")
            else:
                print(f"✗ Redemption failed")
            
            return {
                "success": success,
                "tx_hash": tx_hash.hex(),
                "gas_used": receipt['gasUsed']
            }
            
        except Exception as e:
            print(f"Error: {e}")
            return {"success": False, "error": str(e)}


def load_config():
    with open("pm_api_config.json") as f:
        return json.load(f)


if __name__ == "__main__":
    """Test redemption via proxy"""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python pm_proxy_redeem.py <conditionId>")
        print("Example: python pm_proxy_redeem.py 0x4a629eb456c10ea56e4819f5b54c6727be8010b03c31375fed5b9f100f0dee53")
        sys.exit(1)
    
    condition_id = sys.argv[1]
    
    config = load_config()
    redeemer = ProxyRedeemer(
        config["private_key"],
        config["proxy_address"]
    )
    
    print(f"Checking resolution for: {condition_id}")
    print(f"Proxy wallet: {config['proxy_address']}")
    print()
    
    resolved = redeemer.is_resolved_onchain(condition_id)
    print(f"Resolved on-chain: {resolved}")
    
    if resolved:
        print("\nAttempting redemption via proxy wallet...")
        result = redeemer.redeem_via_proxy(condition_id)
        print(f"\nResult: {result}")
    else:
        print("Market not resolved yet - cannot redeem")

