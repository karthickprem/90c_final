"""
Generate Polymarket API Keys from Private Key
"""

import os
import json

# Install required package first
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
except ImportError:
    print("Installing py-clob-client...")
    os.system("pip install py-clob-client")
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

# Your private key (will be used once to derive API credentials)
PRIVATE_KEY = "0x59937762c465842ccdf918afe680451f91741e76b056adb6e126200c976896ae"

# Polymarket CLOB endpoint
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

def main():
    print("=" * 60)
    print("POLYMARKET API KEY GENERATOR")
    print("=" * 60)
    
    try:
        # Create client with private key
        print("\n1. Connecting to Polymarket...")
        client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
        
        # Derive API credentials
        print("2. Deriving API credentials...")
        creds = client.derive_api_key()
        
        print("3. API credentials generated successfully!")
        print(f"   API Key: {creds.api_key[:20]}...")
        print(f"   Secret: {creds.api_secret[:20]}...")
        print(f"   Passphrase: {creds.api_passphrase[:20]}...")
        
        # Save to secure config file
        config = {
            "api_key": creds.api_key,
            "api_secret": creds.api_secret,
            "api_passphrase": creds.api_passphrase,
            "private_key": PRIVATE_KEY,  # Keep for signing orders
        }
        
        config_path = "pm_api_config.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        
        print(f"\n4. Credentials saved to: {config_path}")
        
        # Test the connection
        print("\n5. Testing API connection...")
        
        # Try to get balance
        try:
            # Get wallet address
            address = client.get_address()
            print(f"   Wallet address: {address}")
        except Exception as e:
            print(f"   Note: {e}")
        
        print("\n" + "=" * 60)
        print("SUCCESS! API keys generated and saved.")
        print("=" * 60)
        
        return config
        
    except Exception as e:
        print(f"\nERROR: {e}")
        print("\nTrying alternative method...")
        
        # Alternative: create API key instead of derive
        try:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
            creds = client.create_api_key()
            
            config = {
                "api_key": creds.api_key,
                "api_secret": creds.api_secret,
                "api_passphrase": creds.api_passphrase,
                "private_key": PRIVATE_KEY,
            }
            
            with open("pm_api_config.json", "w") as f:
                json.dump(config, f, indent=2)
            
            print("SUCCESS with create_api_key!")
            return config
            
        except Exception as e2:
            print(f"Alternative also failed: {e2}")
            return None

if __name__ == "__main__":
    main()

