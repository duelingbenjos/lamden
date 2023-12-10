import os
from argparse import ArgumentParser
import json
from lamden.crypto.block_validator import GENESIS_BLOCK_NUMBER, GENESIS_HLC_TIMESTAMP, GENESIS_PREVIOUS_HASH
from lamden.crypto.canonical import block_hash_from_block, hash_genesis_block_state_changes
from lamden.crypto.wallet import Wallet
from contracting.db.encoder import encode, decode


def main(source_genesis: dict, output_path: str, sk: str):
    cwd = os.getcwd()
    source_genesis_path = os.path.join(cwd, source_genesis)

    with open(source_genesis_path) as f:
        unsigned_genesis_block = json.load(f)

    genesis_block = {
    'hash': block_hash_from_block(GENESIS_HLC_TIMESTAMP, GENESIS_BLOCK_NUMBER, GENESIS_PREVIOUS_HASH),
    'number': GENESIS_BLOCK_NUMBER,
    'hlc_timestamp': GENESIS_HLC_TIMESTAMP,
    'previous': GENESIS_PREVIOUS_HASH,
    'genesis': unsigned_genesis_block['genesis'],
    'origin': {
        'signature': '',
        'sender': ''
        }
    }

    founders_wallet = Wallet(seed=bytes.fromhex(sk))
    genesis_block['origin']['sender'] = founders_wallet.verifying_key
    genesis_block['origin']['signature'] = founders_wallet.sign(hash_genesis_block_state_changes(genesis_block['genesis']))

    with open(output_path, 'w') as f:
        f.write(encode(genesis_block))


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('-sk', type=str, required=True)
    parser.add_argument('-g', type=str, required=True)
    parser.add_argument('-o', type=str, required=True)
    args = parser.parse_args()

    main(source_genesis=args.g, output_path=args.o, sk=args.sk)