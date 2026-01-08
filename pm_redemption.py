"""
Universal Polymarket Redemption Module
Supports both Gnosis Safe and Custom Proxy wallets
"""

import json
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
from typing import Optional, Tuple

RPC_URL = "https://polygon-rpc.com"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# CTF ABI
CTF_ABI = [
    {"inputs": [{"name": "conditionId", "type": "bytes32"}],
     "name": "payoutDenominator", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"}],
     "name": "redeemPositions", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"}
]

# Gnosis Safe ABI (minimal)
SAFE_ABI = [
    {"inputs": [], "name": "getOwners", "outputs": [{"name": "", "type": "address[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getThreshold", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "nonce", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"}, {"name": "operation", "type": "uint8"},
                {"name": "safeTxGas", "type": "uint256"}, {"name": "baseGas", "type": "uint256"},
                {"name": "gasPrice", "type": "uint256"}, {"name": "gasToken", "type": "address"},
                {"name": "refundReceiver", "type": "address"}, {"name": "signatures", "type": "bytes"}],
     "name": "execTransaction", "outputs": [{"name": "success", "type": "bool"}],
     "stateMutability": "payable", "type": "function"}
]

# Custom Proxy ABI
CUSTOM_PROXY_ABI = [
    {"inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"}],
     "name": "execute", "outputs": [{"name": "", "type": "bytes"}],
     "stateMutability": "nonpayable", "type": "function"}
]


class UniversalRedeemer:
    def __init__(self, private_key: str, proxy_address: str):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.private_key = private_key
        self.proxy_addr = Web3.to_checksum_address(proxy_address)
        self.account = Account.from_key(private_key)
        
        # CTF contract
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI
        )
        
        # Detect wallet type
        self.wallet_type = self._detect_wallet_type()
        
        # Initialize appropriate contract interface
        if self.wallet_type == "safe":
            self.proxy = self.w3.eth.contract(address=self.proxy_addr, abi=SAFE_ABI)
        elif self.wallet_type == "custom":
            self.proxy = self.w3.eth.contract(address=self.proxy_addr, abi=CUSTOM_PROXY_ABI)
        else:
            raise Exception(f"Unknown wallet type for {proxy_address}")
    
    def _detect_wallet_type(self) -> str:
        """Auto-detect Safe vs Custom Proxy"""
        # Try Safe methods
        try:
            safe = self.w3.eth.contract(address=self.proxy_addr, abi=SAFE_ABI)
            safe.functions.getOwners().call()
            return "safe"
        except:
            pass
        
        # Try Custom Proxy
        try:
            code = self.w3.eth.get_code(self.proxy_addr)
            execute_selector = Web3.keccak(text="execute(address,uint256,bytes)")[:4]
            if execute_selector in code:
                return "custom"
        except:
            pass
        
        return "unknown"
    
    def is_resolved_onchain(self, condition_id: str) -> bool:
        """Check if market is resolved on-chain"""
        try:
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            payout_denom = self.ctf.functions.payoutDenominator(condition_bytes).call()
            return payout_denom > 0
        except:
            return False
    
    def redeem_position(self, condition_id: str) -> dict:
        """
        Redeem position using appropriate method for wallet type
        """
        if not self.is_resolved_onchain(condition_id):
            return {"success": False, "error": "Not resolved on-chain"}
        
        if self.wallet_type == "safe":
            return self._redeem_via_safe(condition_id)
        elif self.wallet_type == "custom":
            return self._redeem_via_custom_proxy(condition_id)
        else:
            return {"success": False, "error": "Unknown wallet type"}
    
    def _redeem_via_custom_proxy(self, condition_id: str) -> dict:
        """Redeem via Custom Proxy (Magic Link wallets)"""
        try:
            print("Redeeming via Custom Proxy...")
            
            # Build CTF redemption calldata
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
            redeem_calldata = self.ctf.encodeABI(
                fn_name="redeemPositions",
                args=[
                    Web3.to_checksum_address(USDC_ADDRESS),
                    parent_collection,
                    condition_bytes,
                    [1, 2]
                ]
            )
            
            # Call Proxy.execute(CTF, 0, calldata)
            nonce = self.w3.eth.get_transaction_count(self.account.address)
            
            tx = self.proxy.functions.execute(
                Web3.to_checksum_address(CTF_ADDRESS),
                0,
                redeem_calldata
            ).build_transaction({
                'from': self.account.address,
                'nonce': nonce,
                'gas': 400000,
                'gasPrice': self.w3.eth.gas_price,
                'chainId': 137
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            print(f"Tx sent: {tx_hash.hex()}")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            return {
                "success": receipt['status'] == 1,
                "tx_hash": tx_hash.hex(),
                "gas_used": receipt['gasUsed'],
                "method": "custom_proxy"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e), "method": "custom_proxy"}
    
    def _redeem_via_safe(self, condition_id: str) -> dict:
        """Redeem via Gnosis Safe (1-of-1 owner assumed)"""
        try:
            print("Redeeming via Gnosis Safe...")
            
            # Build CTF redemption calldata
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
            redeem_calldata = self.ctf.encodeABI(
                fn_name="redeemPositions",
                args=[
                    Web3.to_checksum_address(USDC_ADDRESS),
                    parent_collection,
                    condition_bytes,
                    [1, 2]
                ]
            )
            
            # Get Safe nonce
            safe_nonce = self.proxy.functions.nonce().call()
            
            # Build Safe execTransaction params
            to = Web3.to_checksum_address(CTF_ADDRESS)
            value = 0
            data = redeem_calldata
            operation = 0  # CALL
            safeTxGas = 0
            baseGas = 0
            gasPrice = 0
            gasToken = "0x" + "00" * 20  # Zero address
            refundReceiver = "0x" + "00" * 20
            
            # Build transaction hash for signing (EIP-712 domain separator)
            # For 1-of-1 Safe, we can use a simple signature
            # NOTE: Full EIP-712 implementation would be more robust
            
            # Simple approach: pack and sign
            tx_hash_data = self.w3.solidityKeccak(
                ['address', 'uint256', 'bytes', 'uint8', 'uint256', 'uint256',
                 'uint256', 'address', 'address', 'uint256'],
                [to, value, data, operation, safeTxGas, baseGas,
                 gasPrice, gasToken, refundReceiver, safe_nonce]
            )
            
            # Sign with EOA
            signed_message = self.account.signHash(tx_hash_data)
            
            # Pack signature (r, s, v)
            signature = signed_message.r.to_bytes(32, 'big') + \
                       signed_message.s.to_bytes(32, 'big') + \
                       signed_message.v.to_bytes(1, 'big')
            
            # Call Safe.execTransaction
            nonce = self.w3.eth.get_transaction_count(self.account.address)
            
            tx = self.proxy.functions.execTransaction(
                to, value, data, operation, safeTxGas, baseGas,
                gasPrice, gasToken, refundReceiver, signature
            ).build_transaction({
                'from': self.account.address,
                'nonce': nonce,
                'gas': 500000,
                'gasPrice': self.w3.eth.gas_price,
                'chainId': 137
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            print(f"Tx sent: {tx_hash.hex()}")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            return {
                "success": receipt['status'] == 1,
                "tx_hash": tx_hash.hex(),
                "gas_used": receipt['gasUsed'],
                "method": "gnosis_safe"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e), "method": "gnosis_safe"}


def load_config():
    with open("pm_api_config.json") as f:
        return json.load(f)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python pm_redemption.py <conditionId>")
        sys.exit(1)
    
    condition_id = sys.argv[1]
    
    config = load_config()
    redeemer = UniversalRedeemer(
        config["private_key"],
        config["proxy_address"]
    )
    
    print(f"Wallet type: {redeemer.wallet_type.upper()}")
    print(f"Proxy: {config['proxy_address']}")
    print(f"Condition: {condition_id}")
    print()
    
    resolved = redeemer.is_resolved_onchain(condition_id)
    print(f"Resolved on-chain: {resolved}")
    
    if resolved:
        print("\nAttempting redemption...")
        result = redeemer.redeem_position(condition_id)
        print(f"\nResult: {result}")
    else:
        print("Market not resolved yet")

