import asyncio
import copy
import gc
import hashlib
import json
import os
import pathlib
import random
import time
import uvloop
import requests

import threading
import psutil

from copy import deepcopy
from contracting.client import ContractingClient
from contracting.db.driver import ContractDriver
from contracting.db.encoder import convert_dict, encode

from lamden import storage, contracts
from lamden.peer import Peer
from lamden.contracts import sync
from lamden.crypto.wallet import Wallet
from lamden.logger.base import get_logger
from lamden.network import Network
from lamden.nodes import system_usage
from lamden.nodes.processing_queue  import TxProcessingQueue
from lamden.nodes.validation_queue  import ValidationQueue
from lamden.nodes.processors import work, block_contender
from lamden.nodes.processors.block_consensus import BlockConsensus
from lamden.nodes.processors.processor import Processor
from lamden.nodes.filequeue import FileQueue
from lamden.nodes.hlc import HLC_Clock
from lamden.crypto.canonical import tx_hash_from_tx, block_from_tx_results, recalc_block_info, create_proof_message_from_tx_results, tx_result_hash_from_tx_result_object, hash_members_list
from lamden.crypto.transaction import get_nonces
from lamden.nodes.events import Event, EventWriter
from lamden.crypto.block_validator import verify_block
from typing import List
from lamden.nodes.catchup import CatchupHandler
from lamden.nodes.missing_blocks import MissingBlocksHandler
from lamden.nodes.rollback_blocks import RollbackBlocksHandler
from lamden.nodes.validate_chain import ValidateChainHandler
from lamden.nodes.member_history import MemberHistoryHandler

from lamden.crypto.transaction import build_transaction
from datetime import datetime, timedelta

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

BLOCK_SERVICE = 'catchup'
GET_LATEST_BLOCK = 'get_latest_block'
GET_BLOCK = "get_block"
GET_CONSTITUTION = "get_constitution"
GET_ALL_PEERS = "get_all_peers"
NEW_BLOCK_SERVICE = 'new_blocks'
NEW_BLOCK_EVENT = 'new_block'
NEW_BLOCK_REORG_EVENT = 'block_reorg'
WORK_SERVICE = 'work'
CONTENDER_SERVICE = 'contenders'
CONSENSUS_SERVICE = 'consensus'

SAFE_BLOCK_HEIGHT = '__safe_block_height'

HARDCODE_NETWORK_START = '2023-08-05T03:59:00.000000000Z_0'

class Node:
    def __init__(self, wallet, bootnodes={}, blocks=None,
                 driver=None, delay=None, client=None, debug=True, testing=False,
                 consensus_percent=None, nonces=None, genesis_block=None, metering=False,
                 tx_queue=None, socket_ports=None, reconnect_attempts=5, join=False, event_writer=None,
                 private_network=False, hardcoded_peers=False, rollback_point=None, run_catchup=True,
                 safe_block_num=None, run_validation=True):

        self.wallet = wallet

        self.main_processing_queue = None
        self.validation_queue = None
        self.check_main_processing_queue_task = None
        self.check_validation_queue_task = None
        self.connectivity_check_task = None
        self.check_for_tx_task = None
        self.run_catchup = run_catchup
        self.run_validation = run_validation

        self.consensus_percent = consensus_percent or 51
        self.processing_delay_secs = delay or {
            'base': 1,
            'self': 0.5
        }

        self.tx_queue = tx_queue if tx_queue is not None else FileQueue()
        self.pause_tx_queue_checking = False

        self.driver = driver if driver is not None else ContractDriver()
        self.nonces = nonces if nonces is not None else storage.NonceStorage()
        self.event_writer = event_writer if event_writer is not None else EventWriter()

        self.blocks = blocks if blocks is not None else storage.BlockStorage()
        self.blocks.member_history.set_secure(wallet=self.wallet)

        self.genesis_block = genesis_block
        self.rollback_point = rollback_point

        self.safe_block_num = safe_block_num

        self.current_thread = threading.current_thread()

        self.log = get_logger(f'[{self.current_thread.name}]Base')
        self.debug = debug
        self.testing = testing

        self.debug_stack = []
        self.debug_processed_hlcs = []
        self.debug_processing_results = []
        self.debug_reprocessing_results = {}
        self.debug_blocks_processed = []
        self.debug_blocks_hard_applied = []
        self.debug_timeline = []
        self.debug_sent_solutions = []
        self.debug_last_checked_main = time.time()
        self.debug_last_checked_val = time.time()
        self.debug_loop_counter = {
            'main': 0,
            'validation': 0,
            'file_check': 0
        }
        self.last_printed_loop_counter = time.time()

        self.log.propagate = debug
        self.hlc_clock = HLC_Clock()

        self.system_monitor = system_usage.SystemUsage()

        self.last_minted_block = None
        self.held_blocks = []
        self.hold_blocks = False

        self.bootnodes = bootnodes

        self.network = Network(
            wallet=wallet,
            socket_ports=socket_ports,
            driver=self.driver,
            block_storage=self.blocks,
            private_network=private_network
        )

        self.validation_queue = ValidationQueue(
            testing=self.testing,
            driver=self.driver,
            debug=self.debug,
            blocks=self.blocks,
            consensus_percent=lambda: self.consensus_percent,
            get_block_by_hlc=self.get_block_by_hlc,
            get_block_from_network=self.get_block_from_network,# Abstract
            hard_apply_block=self.hard_apply_block,                                     # Abstract
            wallet=self.wallet,
            stop_node=self.stop
        )

        self.join = join

        self.client = client or ContractingClient(
            driver=self.driver,
            submission_filename=None
        )

        self.main_processing_queue = TxProcessingQueue(
            testing=self.testing,
            debug=self.debug,
            driver=self.driver,
            client=self.client,
            wallet=self.wallet,
            metering=metering,
            hlc_clock=self.hlc_clock,
            processing_delay=lambda: self.processing_delay_secs,                        # Abstract
            get_last_hlc_in_consensus=self.get_last_hlc_in_consensus,                   # Abstract
            stop_node=self.stop,
            reprocess=self.reprocess,
            check_if_already_has_consensus=self.check_if_already_has_consensus,         # Abstract
            pause_all_queues=self.pause_validation_queue,
            unpause_all_queues=self.unpause_all_queues
        )

        self.total_processed = 0

        self.work_validator = work.WorkValidator(
            wallet=wallet,
            main_processing_queue=self.main_processing_queue,
            hlc_clock=self.hlc_clock,
            get_last_processed_hlc=self.get_last_processed_hlc,
            stop_node=self.stop,
            driver=self.driver,
            nonces=self.nonces
        )

        self.block_contender = block_contender.Block_Contender(
            testing=self.testing,
            debug=self.debug,
            validation_queue=self.validation_queue,
            get_block_by_hlc=self.get_block_by_hlc,
            wallet=self.wallet,
            network=self.network
        )

        self.catchup_handler: CatchupHandler = CatchupHandler(
            network=self.network,
            contract_driver=self.driver,
            block_storage=self.blocks,
            nonce_storage=self.nonces,
            hardcoded_peers=hardcoded_peers
        )

        self.member_history_handler: MemberHistoryHandler = MemberHistoryHandler(
            block_storage=self.blocks,
            network=self.network
        )

        self.missing_blocks_handler: MissingBlocksHandler = MissingBlocksHandler(
            root=self.blocks.root,
            network=self.network,
            contract_driver=self.driver,
            block_storage=self.blocks,
            nonce_storage=self.nonces,
            wallet=self.wallet,
            event_writer=self.event_writer
        )

        self.validate_chain_handler: ValidateChainHandler = ValidateChainHandler(
            block_storage=self.blocks,
            contract_driver=self.driver
        )

        self.block_consensus = BlockConsensus(
            block_storage=self.blocks,
            event_writer=self.event_writer
        )

        self.network.add_service(WORK_SERVICE, self.work_validator)
        self.network.add_service(CONTENDER_SERVICE, self.block_contender)
        self.network.add_service(CONSENSUS_SERVICE, self.block_consensus)

        self.running = False
        self.started = False

        self.reconnect_attempts = reconnect_attempts

        self.network_connectivity_check_timeout = 120

    @property
    def vk(self) -> str:
        return self.wallet.verifying_key

    @property
    def is_running(self) -> bool:
        return self.running

    async def start(self):
        if self.running:
            return

        self.log.warning('Running Node Start Process...')
        # self.print_debug_info()
        # self.log.warning('------------------------------')
        try:
            self.running = True

            # Start the system usage monitor
            if self.debug:
                asyncio.ensure_future(self.system_monitor.start(delay_sec=120))

            if self.rollback_point is not None:
                rollback_blocks_handler: RollbackBlocksHandler = RollbackBlocksHandler(
                    contract_driver=self.driver,
                    block_storage=self.blocks,
                    nonce_storage=self.nonces,
                    wallet=self.wallet,
                    event_writer=self.event_writer
                )
                rollback_blocks_handler.run(rollback_point=self.rollback_point)

            self.set_safe_block_height()

            # Validate the chain
            if self.run_validation:
                self.validate_chain_handler.run()

            # Start the network and wait till it's up
            self.network.start()
            await self.network.starting()

            # Get the genesis block if we don't have it
            if not self.blocks.has_genesis():
                if self.genesis_block is None:
                    self.log.error("Cannot start node without genesis_block. Check documentaion to obtain block.")
                    await self.stop()

                await self.store_genesis_block(genesis_block=self.genesis_block)

            # remove the genesis block from memory
            self.genesis_block = None

            # Connect to all nodes
            if not self.join:
                await self.start_new_network()

                # will till connected to everyone
                await self.network.connected_to_all_peers()

                self.network.refresh_approved_peers_in_cred_provider()
            else:
                await self.join_existing_network()

                # will till connected to everyone
                await self.network.connected_to_all_peers()

            # Start all queues and services
            self.start_validation_queue_task()
            self.start_main_processing_queue_task()

            loop = asyncio.get_event_loop()
            self.check_for_tx_task = loop.create_task(self.check_tx_queue())
            self.connectivity_check_task = loop.create_task(self.connectivity_check())

            # Run catchup unless this was a rollback
            if self.rollback_point is None:
                if self.run_catchup:
                    member_history_task = asyncio.ensure_future(self.member_history_handler.catchup_history())
                    member_history_task.add_done_callback(self.handle_member_history_result)
            else:
                self.rollback_point = None

            self.started = True
            self.log.info('Node has been successfully started!')

        except Exception as err:
            self.log.error(err)
            await self.stop()

    def start_node(self):
        asyncio.ensure_future(self.start())

    def handle_member_history_result(self, future):
        try:
            future.result()

            catchup_task = asyncio.ensure_future(self.catchup_handler.run())
            catchup_task.add_done_callback(self.handle_catchup_result)

        except Exception as error:
            self.log.error(f"An error occurred during member history catchup: {error}")
            asyncio.ensure_future(self.stop())

    def handle_catchup_result(self, future):
        try:
            future.result()
        except Exception as error:
            self.log.error(f"An error occurred during catchup: {error}")
            asyncio.ensure_future(self.stop())


    async def stop(self):
        self.log.info("!!!!!! STOPPING NODE !!!!!!")

        self.running = False


        await self.cancel_checking_all_queues()

        await self.stop_connectivity_check()

        await self.network.stop()
        self.system_monitor.stop()
        await self.system_monitor.stopping()

        self.started = False

        self.log.info("!!!!!! STOPPED NODE !!!!!!")

    async def start_new_network(self):
        self.network.router.refresh_cred_provider_vks(vk_list=[key for key in self.bootnodes])

        self.log.info("Attempting to connect to all peers in constitution...")
        for vk, ip in self.bootnodes.items():
            self.log.info(f'Attempting to connect to peer "{vk[:8]}" @ {ip}')
            self.network.connect_peer(
                ip=ip,
                vk=vk
            )

    async def join_existing_network(self):
        self.network.router.cred_provider.open_messages()

        network_map = {}
        for vk, ip in self.bootnodes.items():
            network_map = await self.network.get_network_map_from_bootnode(vk=vk, ip=ip)
            if not network_map:
                self.log.error(f'Bootnode "{vk[:8]}"@{ip} failed to provide a valid network map...')
                continue
            else:
                self.log.info(f'Received network map: {network_map}')
                break

        assert network_map, "Failed to get a network map from any bootnode."

        peer_list = self.network.network_map_to_node_list(network_map=network_map)
        vk_list = [peer.get('vk') for peer in peer_list]

        self.network.router.refresh_cred_provider_vks(vk_list=vk_list)

        for node in peer_list:
            self.network.connect_peer(ip=node['ip'], vk=node['vk'])


    def send_startup_transaction(self, processor_vk):
        ip = 'lamden_webserver' if processor_vk == self.wallet.verifying_key else self.network.get_node_ip(processor_vk)
        try:
            nonce = json.loads(requests.get(f'http://{ip}:18080/nonce/{self.wallet.verifying_key}', timeout=(10,10)).text)['nonce']
            startup_tx = build_transaction(
                wallet=self.wallet,
                contract='upgrade',
                function='startup',
                kwargs={'lamden_tag': os.getenv('LAMDEN_TAG'), 'contracting_tag': os.getenv('CONTRACTING_TAG')},
                nonce=nonce,
                processor=processor_vk,
                stamps=500
            )
            self.log.info(f'Sending startup transaction... Receiver vk: {processor_vk}, receiver ip: {ip}')
            self.log.info(requests.post(f'http://{ip}:18080', data=startup_tx).json())
        except Exception as e:
            self.log.error(f'An attempt to send startup transaction failed with error: {e}')


    def save_nonce_from_block(self, block: dict):
        payload = block['processed']['transaction']['payload']

        nonce = self.nonces.get_nonce(
            processor=payload['processor'],
            sender=payload['sender']
        )

        if nonce is None:
            nonce = 0

        block_nonce = payload['nonce']

        if block_nonce > nonce:
            self.nonces.set_nonce(
                sender=payload['sender'],
                processor=payload['processor'],
                value=payload['nonce']
            )

    def start_main_processing_queue_task(self):
        self.log.info('STARTING MAIN PROCESSING QUEUE')
        self.check_main_processing_queue_task = asyncio.ensure_future(self.check_main_processing_queue())

    def start_validation_queue_task(self):
        self.log.info('STARTING VALIDATION QUEUE')
        self.check_validation_queue_task = asyncio.ensure_future(self.check_validation_queue())

    async def cancel_checking_all_queues(self):
        self.log.info("!!!!!! STOPPING ALL QUEUES !!!!!!")

        self.log.debug(f'NODE RUNNING: {self.running}')

        await self.stop_connectivity_check()
        self.log.info("!!!!!! check_tx_queue STOPPED !!!!!!")

        if isinstance(self.main_processing_queue, TxProcessingQueue):
            self.main_processing_queue.stop()
            while self.check_main_processing_queue_task and not self.check_main_processing_queue_task.done():
                await asyncio.sleep(0.1)

        self.log.info("!!!!!! main_processing_queue STOPPED !!!!!!")

        if self.validation_queue is not None:
            self.validation_queue.stop()
            while self.check_validation_queue_task and not self.check_validation_queue_task.done():
                await asyncio.sleep(0.1)

        self.log.info("!!!!!! validation_queue STOPPED !!!!!!")

        self.log.info("!!!!!! STOPPED ALL QUEUES !!!!!!")

    async def pause_main_processing_queue(self):
        self.log.info("!!!!!! PAUSING main_processing_queue !!!!!!")
        self.main_processing_queue.pause()
        await self.main_processing_queue.pausing()
        self.log.info("!!!!!! main_processing_queue PAUSED !!!!!!")

    async def pause_validation_queue(self):
        self.log.info("!!!!!! PAUSING validation_queue !!!!!!")
        self.validation_queue.pause()
        await self.validation_queue.pausing()
        self.log.info("!!!!!! validation_queue PAUSED !!!!!!")

    def unpause_all_queues(self):
        self.log.info("!!!!!! RESUMING ALL QUEUES !!!!!!")
        self.main_processing_queue.unpause()
        self.validation_queue.unpause()
        self.log.info(f"main_processing_queue paused: {self.main_processing_queue.paused}")
        self.log.info(f"validation_queue paused: {self.validation_queue.paused}")

    async def pause_all_queues(self):
        self.log.info("!!!!!! PAUSING ALL QUEUES !!!!!!")
        await self.pause_main_processing_queue()
        await self.pause_validation_queue()

    def pause_tx_queue(self):
        self.pause_tx_queue_checking = True

    def unpause_tx_queue(self):
        self.pause_tx_queue_checking = False

    async def check_tx_queue(self):
        while self.running and not self.pause_tx_queue_checking:
            if len(self.tx_queue) > 0:
                self.log.debug("Calling Check TX File Queue")
                tx_from_file = self.tx_queue.pop(0)
                # TODO sometimes the tx info taken off the filequeue is None, investigate
                self.log.info(f'GOT TX FROM FILE {tx_from_file}')
                if tx_from_file is not None:
                    tx_message = self.make_tx_message(tx=tx_from_file)

                    #if tx_message.get('hlc_timestamp') < HARDCODE_NETWORK_START:
                    #    self.log.warning("Received tx before network start date.")
                    #    return

                    # send the tx to the rest of the network
                    asyncio.ensure_future(self.network.publisher.async_publish(topic_str=WORK_SERVICE, msg_dict=tx_message))

                    # add this tx the processing queue so we can process it
                    self.main_processing_queue.append(tx=tx_message)

            self.debug_loop_counter['file_check'] = self.debug_loop_counter['file_check'] + 1
            await asyncio.sleep(0.1)

    async def stop_check_tx_queue_task(self):
        if self.check_for_tx_task is not None:
            self.check_for_tx_task.cancel()

            try:
                await asyncio.gather(self.check_for_tx_task, return_exceptions=True)
            except asyncio.CancelledError:
                print("connectivity_check_task was cancelled")
            except Exception as e:
                print(f"Unexpected exception: {e}")

        self.check_for_tx_task = None


    async def connectivity_check(self):
        while self.running:
            await asyncio.sleep(self.network_connectivity_check_timeout)

            if not await self.network.check_connectivity():
                e = Event(topics=['network_error'], data={
                    'node_vk': self.wallet.verifying_key,
                    'bootnode_ips': self.network.get_bootnode_ips()
                })
                try:
                    self.event_writer.write_event(e)
                    self.log.info(f'Successfully sent "network_error" event: {e.__dict__}')
                    break
                except Exception as err:
                    self.log.error(f'Failed to write "network_error" event: {err}')

    async def stop_connectivity_check(self):
        if self.connectivity_check_task is not None:
            self.connectivity_check_task.cancel()

            try:
                await asyncio.gather(self.connectivity_check_task, return_exceptions=True)
            except asyncio.CancelledError:
                print("connectivity_check_task was cancelled")
            except Exception as e:
                print(f"Unexpected exception: {e}")

        self.connectivity_check_task = None

    async def check_main_processing_queue(self):
        self.main_processing_queue.start()

        while self.main_processing_queue.running:
            if len(self.main_processing_queue) > 0 and self.main_processing_queue.active:
                self.main_processing_queue.start_processing()
                await self.process_main_queue()
                self.main_processing_queue.stop_processing()

            self.debug_loop_counter['main'] = self.debug_loop_counter['main'] + 1
            await asyncio.sleep(0.1)

        self.log.info(f'Exited Check Main Processing Queue.')

    async def check_validation_queue(self):
        self.validation_queue.start()

        while self.validation_queue.running:
            if self.validation_queue.active:
                #self.log.debug('[START] check_validation_queue')
                self.validation_queue.start_processing()
                # TODO Alter this method to process just the earliest HLC
                await self.validation_queue.process_all()
                self.validation_queue.stop_processing()
                #self.log.debug('[END] check_validation_queue')

            self.debug_loop_counter['validation'] = self.debug_loop_counter['validation'] + 1
            await asyncio.sleep(0.1)

        self.log.info(f'Exited Check Validation Queue.')

    async def process_main_queue(self):

        try:
            processing_results = await self.main_processing_queue.process_next()

            if processing_results and self.running:
                hlc_timestamp = processing_results.get('hlc_timestamp')
                self.soft_apply_current_state(hlc_timestamp=hlc_timestamp)

                if self.testing:
                    self.debug_processing_results.append(processing_results)

                if hlc_timestamp <= self.get_last_hlc_in_consensus():
                    block = self.blocks.get_block(v=hlc_timestamp)
                    my_result_hash = self.make_result_hash_from_processing_results(
                        processing_results=processing_results
                    )
                    block_result_hash = block['processed']['hash']

                    if my_result_hash != block_result_hash:
                        await self.reprocess(tx=processing_results['tx_result']['transaction'])
                else:
                    processing_results = self.add_proof_to_processing_results(processing_results=processing_results)
                    self.store_solution_and_send_to_network(processing_results=processing_results)

        except Exception as err:
            self.log.error(err)

    def add_proof_to_processing_results(self, processing_results: dict) -> dict:
        # Create merkle
        tx_result = processing_results.get('tx_result')
        hlc_timestamp = processing_results.get('hlc_timestamp')
        rewards = processing_results.get('rewards')
        members = self.network.get_node_list() or []

        if not tx_result or not hlc_timestamp or not rewards:
            raise ValueError('Invalid processing results. Cannot add proof.')

        sign_info = self.sign_tx_results(
            tx_result=tx_result,
            hlc_timestamp=hlc_timestamp,
            rewards=rewards,
            members=members
        )

        processing_results['proof'] = sign_info

        return processing_results

    def sign_tx_results(self, tx_result, hlc_timestamp, rewards, members):
        proof_details = create_proof_message_from_tx_results(
            tx_result=tx_result,
            hlc_timestamp=hlc_timestamp,
            rewards=rewards,
            members=members
        )

        signature = self.wallet.sign(proof_details.get('message'))

        return {
            'signature': signature,
            'signer': self.wallet.verifying_key,
            'members_list_hash': proof_details.get('members_list_hash'),
            'num_of_members': proof_details.get('num_of_members'),
        }

    def store_solution_and_send_to_network(self, processing_results):
        processing_results = json.loads(encode(processing_results))
        self.send_solution_to_network(processing_results=processing_results)

        processing_results['proof']['tx_result_hash'] = self.make_result_hash_from_processing_results(
            processing_results=processing_results
        )
        self.validation_queue.append(
            processing_results=processing_results
        )


    def make_result_hash_from_processing_results(self, processing_results: dict) -> dict:
        return tx_result_hash_from_tx_result_object(
            tx_result=processing_results['tx_result'],
            hlc_timestamp=processing_results['hlc_timestamp'],
            rewards=processing_results['rewards']
        )

    def send_solution_to_network(self, processing_results):
        asyncio.ensure_future(self.network.publisher.async_publish(topic_str=CONTENDER_SERVICE, msg_dict=processing_results))

    def soft_apply_current_state(self, hlc_timestamp):
        try:
            self.driver.soft_apply(hcl=hlc_timestamp)
            gc.collect()
        except Exception as err:
            self.log.error(err)

    def make_tx_message(self, tx):
        hlc_timestamp = self.hlc_clock.get_new_hlc_timestamp()

        tx_hash = tx_hash_from_tx(tx=tx)

        signature = self.wallet.sign(f'{tx_hash}{hlc_timestamp}')

        return {
            'tx': tx,
            'hlc_timestamp': hlc_timestamp,
            'signature': signature,
            'sender': self.wallet.verifying_key
        }

    def update_block_db(self, block):
        # NOTE: write it directly to disk if it's greater then current
        if int(block.get('number')) >= self.get_current_height():
            self.driver.driver.set(storage.LATEST_BLOCK_HASH_KEY, block['hash'])
            self.driver.driver.set(storage.LATEST_BLOCK_HEIGHT_KEY, block['number'])

    def get_state_changes_from_block(self, block):
        try:
            if self.blocks.is_genesis_block(block):
                return block.get('genesis', [])
            else:
                return block['processed'].get('state', [])
        except Exception:
            return []

    def apply_state_changes_from_block(self, block):
        if self.blocks.is_genesis_block(block):
            state_changes = block.get('genesis', [])
        else:
            try:
                state_changes = block['processed'].get('state', [])
            except Exception as err:
                print(err)

        rewards = block.get('rewards', [])

        hlc_timestamp = block.get('hlc_timestamp')

        for s in state_changes:
            if type(s['value']) is dict:
                s['value'] = convert_dict(s['value'])

            self.driver.set(s['key'], s['value'])

        for s in rewards:
            if type(s['value']) is dict:
                s['value'] = convert_dict(s['value'])

            self.driver.set(s['key'], s['value'])

        self.soft_apply_current_state(hlc_timestamp=hlc_timestamp)

        pending_delta = self.driver.hard_apply_one(hlc=hlc_timestamp)
        self.driver.bust_cache(writes=pending_delta.get('writes'))


    # TODO: move to state manager in the future.
    def is_known_masternode(self, processor_vk):
        return processor_vk in (self.driver.driver.get('masternodes.S:members') or [])

    async def hard_apply_block(self, processing_results: dict = None, block: dict = None, force=False):
        if block is not None:
            block_num = block.get("number")
            hlc_timestamp = block.get('hlc_timestamp')
            latest_block = self.blocks.get_latest_block()

            if not force and int(block_num) > 0:
                if latest_block.get("hlc_timestamp") >= hlc_timestamp:
                    self.log.warning(f'Tried to hard apply earlier block.  Block {block.get("number")} ignored.')
                    return

                block_previous_hash = block.get('previous')

                # Check if this would be the "next" block by checking previous hash
                if latest_block and latest_block.get('hash') != block_previous_hash:

                    # If it is not, then we need to do catchup.
                    self.log.error(f'Tried to Hard Apply a block {block.get("number")} with invalid previous hash')
                    self.log.warning(f'was expecting hash {latest_block.get("hash")} and got {block_previous_hash}')

                    if self.catchup_handler.running:
                        self.log.warning("Attempted to run catchup but it was already running...")
                    else:
                        await self.catchup_handler.run()

                    return

            # Apply the state changes from the block to the db
            self.apply_state_changes_from_block(block)

            self.hard_apply_store_block(block=block)
            self.hard_apply_block_finish(block=block)

            return block

        else:
            if processing_results is None:
                raise AttributeError('Processing Results are NONE')

            hlc_timestamp = processing_results.get('hlc_timestamp')
            processor = processing_results['tx_result']['transaction']['payload']['processor']

            if not self.is_known_masternode(processor):
                self.log.error(f'Processor {processor[:8]} is not a known masternode. Dropping {hlc_timestamp}')
                return

            # Get any blocks that have been commited that are later than this hlc_timestamp
            later_blocks = self.blocks.get_later_blocks(hlc_timestamp=hlc_timestamp)

            # If there are later blocks then we need to process them
            if len(later_blocks) == 0:
                block = self.hard_apply_processing_results(processing_results=processing_results)
            else:
                block = self.hard_apply_has_later_blocks(later_blocks=later_blocks, processing_results=processing_results)

            return block


    def hard_apply_has_later_blocks(self, later_blocks: list, processing_results: dict = None, block: dict = None):
        # Get the block number of the block right after where we want to put this tx this will be the block number
        # for our new block
        next_block_num = int(later_blocks[0].get('number'))

        # get the block currently previous to the next block
        prev_block = self.blocks.get_previous_block(v=next_block_num)

        if prev_block is None:
            prev_block = storage.BLOCK_0

        if block is None:
            hlc_timestamp = processing_results.get('hlc_timestamp')

            new_block = block_from_tx_results(
                processing_results=processing_results,
                proofs=self.validation_queue.get_proofs_from_results(hlc_timestamp=hlc_timestamp),
                prev_block_hash=prev_block.get('hash'),
                wallet=self.wallet
            )
        else:
            new_block = block

        for i in range(len(later_blocks)):
            if i is 0:
                prev_block_in_list = new_block
            else:
                prev_block_in_list = later_blocks[i - 1]

            later_blocks[i] = recalc_block_info(
                block=later_blocks[i],
                new_prev_hash=prev_block_in_list.get('hash')
            )

        # Apply the state changes from the block to the db
        self.apply_state_changes_from_block(new_block)

        # Store the new block in the block db
        self.blocks.store_block(new_block)

        # Emit a block reorg event

        # create a NEW_BLOCK_REORG_EVENT
        encoded_block = encode(new_block)
        encoded_block = json.loads(encoded_block)
        try:
            self.event_writer.write_event(Event(
                topics=[NEW_BLOCK_REORG_EVENT],
                data=encoded_block
            ))
        except Exception as e:
            self.log.error(f'Failed to write "{NEW_BLOCK_REORG_EVENT}" event: {e}')

        # reapply the state changes in the later blocks and re-save them
        for block in later_blocks:
            # Apply the state changes for this block to the db
            self.apply_state_changes_from_block(block)

            if self.hold_blocks:
                # Hold blocks till after we are caught up and then apply state
                self.held_blocks.append(encoded_block)
            else:
                self.blocks.store_block(block)

                # create a NEW_BLOCK_REORG_EVENT
                encoded_block = encode(block)
                encoded_block = json.loads(encoded_block)
                try:
                    self.event_writer.write_event(Event(
                        topics=[NEW_BLOCK_REORG_EVENT],
                        data=encoded_block
                    ))
                except Exception as e:
                    self.log.error(f'Failed to write "{NEW_BLOCK_REORG_EVENT}" event: {e}')

        self.hard_apply_block_finish(block=new_block)

        return new_block

    def hard_apply_processing_results(self, processing_results: dict):
        hlc_timestamp = processing_results.get('hlc_timestamp')

        prev_block = self.blocks.get_previous_block(v=hlc_timestamp)

        if prev_block is None:
            prev_block = storage.BLOCK_0

        new_block = block_from_tx_results(
            processing_results=processing_results,
            proofs=self.validation_queue.get_proofs_from_results(hlc_timestamp=hlc_timestamp),
            prev_block_hash=prev_block.get('hash'),
            wallet=self.wallet
        )

        consensus_matches_me = self.validation_queue.consensus_matches_me(hlc_timestamp=hlc_timestamp)

        # Hard apply this hlc_timestamps state changes
        if hlc_timestamp in self.driver.pending_deltas and consensus_matches_me:
            pending_delta = self.driver.hard_apply_one(hlc=hlc_timestamp)
            self.driver.bust_cache(writes=pending_delta.get('writes'))
        else:
            self.apply_state_changes_from_block(new_block)

        self.hard_apply_store_block(block=new_block)
        self.hard_apply_block_finish(block=new_block)

        self.network.publisher.announce_new_block(block=new_block)
        self.block_consensus.post_minted_block(block=new_block)

        return new_block

    def hard_apply_store_block(self, block: dict):
        self.log.info(f'[HARD APPLY] {block.get("number")}')

        # Store the block in the block db
        encoded_block = encode(block)
        encoded_block = json.loads(encoded_block)

        if self.hold_blocks:
            # Hold blocks till after we are caught up and then apply state
            self.held_blocks.append(encoded_block)
        else:
            self.blocks.store_block(copy.copy(encoded_block))

            # Set the current block hash and height
            # self.update_block_db(block=encoded_block)

            try:
                # create New Block Event
                self.event_writer.write_event(Event(
                    topics=[NEW_BLOCK_EVENT],
                    data=encoded_block
                ))
            except Exception as e:
                self.log.error(f'Failed to write "{NEW_BLOCK_EVENT}" event: {e}')

    def hard_apply_block_finish(self, block: dict):
        state_changes = self.get_state_changes_from_block(block=block)
        if not self.blocks.is_genesis_block(block=block):
            self.check_peers(state_changes=state_changes, hlc_timestamp=block.get('hlc_timestamp'), block_num=block.get('number'))
            self.check_upgrade(state_changes=state_changes)

        gc.collect()

        # check to see if we need to process any missing blocks.
        asyncio.ensure_future(self.missing_blocks_handler.run())

    def check_upgrade(self, state_changes: list):
        for change in state_changes:
            if change['key'] == 'upgrade.S:lamden_tag' or change['key'] == 'upgrade.S:contracting_tag':
                self.produce_upgrade_event()
                break

    def produce_upgrade_event(self):
        cur_lam_tag = os.getenv('LAMDEN_TAG', '')
        cur_con_tag = os.getenv('CONTRACTING_TAG', '')
        new_lam_tag = self.driver.driver.get('upgrade.S:lamden_tag') or ''
        new_con_tag = self.driver.driver.get('upgrade.S:contracting_tag') or ''

        should_upgrade = (new_lam_tag != '' and new_lam_tag != cur_lam_tag) or (new_con_tag != '' and new_con_tag != cur_con_tag)
        if not should_upgrade:
            self.log.info(f'Ignored upgrade proposal: lamden ({cur_lam_tag}->{new_lam_tag}), contracting ({cur_con_tag}->{new_con_tag})')
            return

        e = Event(topics=['upgrade'], data={
            'node_vk': self.wallet.verifying_key,
            'lamden_tag': new_lam_tag,
            'contracting_tag': new_con_tag,
            'bootnode_ips': self.network.get_bootnode_ips(),
            'utc_when': str(datetime.utcnow() + timedelta(minutes=10 + self.network.get_node_list().index(self.wallet.verifying_key) * 10))
        })
        try:
            self.event_writer.write_event(e)
            self.log.info(f'Successfully sent "upgrade" event: {e.__dict__}')
        except Exception as err:
            self.log.error(f'Failed to write "upgrade" event: {err}')

    def check_peers(self, hlc_timestamp: str, state_changes: list, block_num: str):
        exiled_peers = []

        for change in state_changes:
            if change['key'] == 'masternodes.S:members':
                exiled_peers = self.network.get_exiled_peers()
                self.network.refresh_approved_peers_in_cred_provider()
                break

        if len(exiled_peers) == 0:
            return

        if self.wallet.verifying_key in exiled_peers:
            self.log.fatal('I was voted out from the network... Shutting down!')
            asyncio.ensure_future(self.stop())
        else:
            for vk in exiled_peers:
                self.network.revoke_access_and_remove_peer(peer_vk=vk)
                self.validation_queue.clear_solutions(node_vk=vk, max_hlc=hlc_timestamp)


# Re-processing CODE
    async def reprocess(self, tx):
        # make a copy of all the values before reprocessing, so we can compare transactions that are rerun
        pending_delta_history = deepcopy(self.driver.pending_deltas)

        self.log.debug(f"Reprocessing {len(pending_delta_history.keys())} Transactions")

        # Get HLC of tx that needs to be run
        new_tx_hlc_timestamp = tx.get("hlc_timestamp")

        # Get the read history of all transactions that were run
        changed_keys_list = []

        # Add the New HLC to the list of hlcs so we can process it in order
        pending_delta_items = list(pending_delta_history.keys())
        pending_delta_items.append(new_tx_hlc_timestamp)
        pending_delta_items.sort()

        # Check the read_history if all HLCs that were processed, in order of oldest to newest
        for index, read_history_hlc in enumerate(pending_delta_items):

            # if this is the transaction we have to rerun,
            if read_history_hlc == new_tx_hlc_timestamp:
                try:
                    # rollback to this point
                    self.rollback_drivers(hlc_timestamp=new_tx_hlc_timestamp)

                    # Process the transaction
                    processing_results = self.main_processing_queue.process_tx(tx=tx)
                    self.soft_apply_current_state(hlc_timestamp=new_tx_hlc_timestamp)
                    changed_keys_list = list(deepcopy(self.driver.pending_deltas[new_tx_hlc_timestamp].get('writes')))
                    self.store_solution_and_send_to_network(processing_results=processing_results)
                    continue
                except Exception as err:
                    self.log.error(err)

            # if the hlc is less than the hlc we need to run then leave it alone, it won't need any changes
            if read_history_hlc < new_tx_hlc_timestamp:
                continue

            # If HLC is greater than rollback point check it for reprocessing
            if read_history_hlc > new_tx_hlc_timestamp:
                try:
                    self.reprocess_hlc_simple(hlc_timestamp=read_history_hlc)
                except Exception as err:
                    self.log.error(err)

    def reprocess_after_earlier_block(self, new_keys_list):
        # make a copy of all the values before reprocessing, so we can compare transactions that are rerun
        pending_delta_history = deepcopy(self.driver.pending_deltas)

        self.log.debug(f"Reprocessing {len(pending_delta_history.keys())} Transactions")

        # Get the read history of all transactions that were run
        changed_keys_list = new_keys_list

        # Get and sort the list of HLCs so we can process it in order
        pending_delta_items = list(self.driver.pending_deltas.keys())
        pending_delta_items.sort()

        # Check the read_history if all HLCs that were processed, in order of oldest to newest
        for index, read_history_hlc in enumerate(pending_delta_items):
            try:
                self.reprocess_hlc(
                    hlc_timestamp=read_history_hlc,
                    pending_deltas=pending_delta_history.get(read_history_hlc, {}),
                    changed_keys_list=changed_keys_list
                )
            except Exception as err:
                self.log.error(err)

    def reprocess_hlc_simple(self, hlc_timestamp):
        recreated_tx_message = self.validation_queue.get_recreated_tx_message(hlc_timestamp)
        if recreated_tx_message is None:
            return

        processing_results = self.main_processing_queue.process_tx(tx=recreated_tx_message)
        self.soft_apply_current_state(hlc_timestamp=hlc_timestamp)

        new_result_hash = self.make_result_hash_from_processing_results(
            processing_results=processing_results
        )

        previous_result_hash = self.validation_queue.get_result_hash_for_vk(
            hlc_timestamp=hlc_timestamp,
            node_vk=self.vk
        )

        if previous_result_hash is None or new_result_hash != previous_result_hash:
            self.store_solution_and_send_to_network(processing_results=processing_results)

    def reprocess_hlc(self, hlc_timestamp, pending_deltas, changed_keys_list):
        # Create a flag to determine there were any matching keys
        key_in_change_list = False
        prev_pending_deltas = pending_deltas

        # Get the keys that tx read from
        read_history_keys = list(prev_pending_deltas.get('reads', {}).keys())

        # Look at each key this hlc read and see if it was a key that was changed earlier either by the hlc
        # that triggered this reprocessing or due to reprocessing
        for read_key in read_history_keys:
            if read_key in changed_keys_list:
                # Flag that we matched a key
                key_in_change_list = True
                break

        if key_in_change_list:
            # Get the transaction info from the validation results queue
            recreated_tx_message = self.validation_queue.get_recreated_tx_message(hlc_timestamp)

            try:
                # Reprocess the transaction
                processing_results = self.main_processing_queue.process_tx(tx=recreated_tx_message)

                # Create flag to know if anything changes so we can later resend our new results to the
                # network
                re_send_to_network = False

                # Check if the previous run had any pending deltas
                pending_deltas_writes = prev_pending_deltas.get('writes', {})
                pending_writes = self.driver.pending_writes

                # If there were no previous writes but reprocessing had writes then just add then all to
                # the changed_keys_list and flag to resend our results to the network
                if len(pending_deltas_writes) is 0 and len(pending_writes) > 0:
                    # Flag that we need to resend our results to the network
                    re_send_to_network = True

                    # Add all the keys from the pending_writes to the changed_keys_list
                    for pending_writes_key in pending_writes.keys():
                        if pending_writes_key not in changed_keys_list:
                            changed_keys_list.append(pending_writes_key)

                # If there WERE writes before AND reprocessing had no writes then add all the before
                # writes to the changed_keys_list and flag to resend our results to the network
                if len(pending_deltas_writes) > 0 and len(pending_writes) is 0:

                    # Flag that we need to resend our results to the network
                    re_send_to_network = True

                    # Add all the keys from the pending_writes to the changed_keys_list
                    for pending_deltas_key in pending_deltas_writes.keys():
                        if pending_deltas_key not in changed_keys_list:
                            changed_keys_list.append(pending_deltas_key)

                # If there were writes previously and after reprocessing then compare then to see if
                # anything changed
                if len(pending_deltas_writes) > 0 and len(pending_writes) > 0:

                    # check the value of each key written during processing against the value of the
                    # previous run
                    for pending_writes_key, new_write_value in pending_writes.items():
                        has_changed = False
                        # Removed this value from the dict so we can see if there are leftovers afterwards
                        prev_write_deltas = pending_deltas_writes.pop(pending_writes_key, None)

                        if prev_write_deltas is None:
                            has_changed = True
                        else:
                            prev_write_value = prev_write_deltas[1]
                            if prev_write_value != new_write_value:
                                has_changed = True

                        if has_changed:
                            # Processing results produced changed results so add this key to the changed
                            # key list so we can check it against the reads of later hlcs in reprocessing
                            if pending_writes_key not in changed_keys_list:
                                changed_keys_list.append(pending_writes_key)

                            # Set flag to sent new results to the network
                            re_send_to_network = True

                    # Check if there are any pending deltas we didn't deal with. This is a situation where
                    # there were writes that happened previously and not during reprocessing
                    if len(pending_deltas_writes) > 0:

                        # Add all the the extra keys to the changed key list because they will now be None
                        # and could effect transactions later on
                        for pending_deltas_key in pending_deltas_writes.keys():
                            changed_keys_list.append(pending_deltas_key)

                # If there were changes to the writes above then we need to re-communicate our results to the
                # rest of the nodes
                if re_send_to_network:
                    # Processing results produced new results so add this key to the changed
                    # key list so we can check it against the reads of later hlcs in reprocessing
                    self.log.debug({"RESENDING_TO_NETWORK": processing_results})
                    self.store_solution_and_send_to_network(processing_results=processing_results)

            except Exception as err:
                self.log.error(err)
        else:
            for pending_delta_key, pending_delta_value in pending_deltas.items():
                self.driver.pending_writes[pending_delta_key] = pending_delta_value[1]

        self.soft_apply_current_state(hlc_timestamp=hlc_timestamp)

    def rollback_drivers(self, hlc_timestamp):
        # Roll back the current state to the point of the last block consensus
        self.log.debug(f"Length of Pending Deltas BEFORE {len(self.driver.pending_deltas.keys())}")
        self.log.debug(f"rollback to hlc_timestamp: {hlc_timestamp}")

        if hlc_timestamp is None:
            # Returns to disk state which should be whatever it was prior to any write sessions
            self.driver.cache.clear()
            self.driver.reads = set()
            self.driver.pending_writes.clear()
            self.driver.pending_deltas.clear()
        else:
            to_delete = []
            for _hlc, _deltas in sorted(self.driver.pending_deltas.items())[::-1]:
                # Clears the current reads/writes, and the reads/writes that get made when rolling back from the
                # last HLC
                self.driver.reads = set()
                self.driver.pending_writes.clear()


                if _hlc < hlc_timestamp:
                    self.log.debug(f"{_hlc} is less than {hlc_timestamp}, breaking!")
                    # if we are less than the HLC then top processing anymore, this is our rollback point
                    break
                else:
                    # if we are still greater than or equal to then mark this as delete and rollback its changes
                    to_delete.append(_hlc)
                    # Run through all state changes, taking the second value, which is the post delta
                    for key, delta in _deltas['writes'].items():
                        # self.set(key, delta[0])
                        self.driver.cache[key] = delta[0]

            # Remove the deltas from the set
            self.log.debug(to_delete)
            [self.driver.pending_deltas.pop(key) for key in to_delete]

        self.log.debug(f"Length of Pending Deltas AFTER {len(self.driver.pending_deltas.keys())}")

    # Put into 'super driver'
    def get_block_by_hlc(self, hlc_timestamp):
        return self.blocks.get_block(v=hlc_timestamp)

    # Put into 'super driver'
    async def get_block_from_network(self, hlc_timestamp):
        block = await self.catchup_handler.source_block_from_peers(
            fetch_type='specific',
            block_num=self.hlc_clock.get_nanos(timestamp=hlc_timestamp)
        )

        return block

    # Put into 'super driver'
    def get_block_by_number(self, block_number: str) -> dict:
        return self.blocks.get_block(v=int(block_number))

    async def store_genesis_block(self, genesis_block: dict) -> bool:
        self.log.info('Processing Genesis Block.')

        if self.blocks.total_blocks() > 0:
            self.network.refresh_approved_peers_in_cred_provider()
            self.log.warning('Genesis Block provided but this node already has blocks, continuing startup process...')
            return

        self.driver.clear_pending_state()

        await self.hard_apply_block(block=genesis_block)

    def should_process(self, block):
        try:
            pass
            # self.log.info(f'Processing block #{block.get("number")}')
        except:
            self.log.error('Malformed block :(')
            return False
        # Test if block failed immediately
        if block == {'response': 'ok'}:
            return False

        if block['hash'] == 'f' * 64:
            self.log.error('Failed Block! Not storing.')
            return False

        return True

    # This is the height where the new proofs will start being validated. block below will use old_block validation
    def set_safe_block_height(self):
        if self.safe_block_num is not None:
            self.safe_block_num = int(self.safe_block_num)
            self.log.info(f"Setting safe block height to {self.safe_block_num}")
            self.driver.driver.set('__safe_block_height', self.safe_block_num)
        else:
            safe_block_height = self.driver.driver.get('__safe_block_height')
            if safe_block_height is None:
                self.log.info(f"Initializing safe_block_height to -1.")
                self.driver.driver.set('__safe_block_height', -1)

        safe_block_height = int(self.driver.driver.get('__safe_block_height'))
        self.catchup_handler.safe_block_num = safe_block_height
        self.missing_blocks_handler.safe_block_num = safe_block_height
        self.validate_chain_handler.safe_block_num = safe_block_height

    # Put into 'super driver'
    def get_current_height(self) -> int:
        return self.blocks.get_latest_block_number()

    # Put into 'super driver'
    def get_current_hash(self) -> str:
        return self.blocks.get_latest_block_hash()

    # Put into 'super driver'
    def get_latest_block(self):
        latest_block = self.blocks.get_latest_block()
        return latest_block

    def get_last_processed_hlc(self):
        return self.main_processing_queue.last_processed_hlc

    def get_last_hlc_in_consensus(self):
        return self.validation_queue.last_hlc_in_consensus

    def is_next_block(self, previous_hash):
        '''
        self.log.debug({
            'current_hash': self.get_consensus_hash(),
            'previous_hash': previous_hash
        })
        '''
        return previous_hash == self.get_consensus_hash()

    def check_if_already_has_consensus(self, hlc_timestamp):
        return self.validation_queue.hlc_has_consensus(hlc_timestamp=hlc_timestamp)

    def print_debug_info(self):
        # Get the process information
        process = psutil.Process(os.getpid())
        process_info = {
            "pid": process.pid,
            "name": process.name(),
            "exe": process.exe(),
            "create_time": process.create_time(),
            "status": process.status(),
            "memory_info": process.memory_info(),
            "cpu_percent": process.cpu_percent(),
        }

        # Get the thread information
        thread = threading.current_thread()
        thread_info = {
            "tid": thread.ident,
            "name": thread.name,
            "is_alive": thread.is_alive(),
        }

        # Print the process and thread information
        self.log.debug("Process Information:")
        for key, value in process_info.items():
            self.log.debug(f"{key}: {value}")

        self.log.debug("\nThread Information:")
        for key, value in thread_info.items():
            self.log.debug(f"{key}: {value}")