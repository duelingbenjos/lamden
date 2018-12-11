import timeit
from collections import defaultdict
from cilantro.logger import get_logger
from cilantro.constants.zmq_filters import *
from cilantro.protocol.reactor.lsocket import LSocket
from cilantro.storage.vkbook import VKBook
from cilantro.storage.state import StateDriver
from cilantro.nodes.masternode.master_store import MasterOps
from cilantro.messages.block_data.block_data import BlockData, BlockMetaData
from cilantro.messages.block_data.state_update import BlockIndexRequest, BlockIndexReply, BlockDataRequest


class CatchupUtil:
    @classmethod
    def process_catch_up_idx(cls, vk = None, curr_blk_hash = None ):
        """
        API gets latest hash requester has and responds with delta block index

        :param vk: mn or dl verifying key
        :param curr_blk_hash:
        :return:
        """

        # check if requester is master or del

        valid_node = bool(VKBook.get_masternodes().index(vk)) or bool(VKBook.get_delegates().index(vk))

        if valid_node is True:
            given_blk_num = MasterOps.get_blk_num_frm_blk_hash(blk_hash = curr_blk_hash)
            latest_blk = MasterOps.get_blk_idx(n_blks = 1)
            latest_blk_num = latest_blk.get('blockNum')

            if given_blk_num == latest_blk_num:
                cls.log.debug('given block is already latest')
                return None
            else:
                idx_delta = MasterOps.get_blk_idx(n_blks = (latest_blk_num - given_blk_num))
                return idx_delta

        assert valid_node is True, "invalid vk given key is not of master or delegate dumpting vk {}".format(vk)

    @classmethod
    def process_received_idx(cls, blk_idx_dict = None):
        """
        API goes list dict and sends out blk req for each blk num
        :param blk_idx_dict:
        :return:
        """
        last_elm_curr_list = sorted(cls.block_index_delta.keys())[-1]
        last_elm_new_list = sorted(blk_idx_dict.keys())[-1]

        if last_elm_curr_list > last_elm_new_list:
            cls.log.critical("incoming block delta is stale ignore continue wrk on old")
            return

        if last_elm_curr_list == last_elm_new_list:
            cls.log.info("delta is same returning")
            return

        if last_elm_curr_list < last_elm_new_list:
            cls.log.critical("we have stale list update working list ")
            cls.block_index_delta = blk_idx_dict
            last_elm_curr_list = last_elm_new_list

        while cls.send_req_blk_num < last_elm_curr_list:
            # look for active master in vk list
            avail_copies = len(cls.block_index_delta[cls.send_req_blk_num])
            if avail_copies < REPLICATION:
                cls.log.critical("block is under protected needs to re protect")

            while avail_copies > 0:
                vk = cls.block_index_delta[cls.send_req_blk_num][avail_copies - 1]
                if vk in VKBook.get_masternodes():
                    CatchupManager._send_block_data_req(mn_vk = vk, req_blk_num = cls.send_req_blk_num)
                    break
                avail_copies = avail_copies - 1  # decrement count check for another master

            cls.send_req_blk_num += 1
            # TODO we should somehow check time out for these requests


    @classmethod
    def process_received_block( cls, block = None ):
        block_dict = MDB.get_dict(block)
        update_blk_result = bool(MasterOps.evaluate_wr(entry = block_dict))
        assert update_blk_result is True, "failed to update block"
        return update_blk_result


class CatchupManager:
    def __init__(self, verifying_key: str, pub_socket: LSocket, router_socket: LSocket, store_full_blocks=True):
        self.log = get_logger("CatchupManager")
        self.catchup_state = False
        self.pub, self.router = pub_socket, router_socket
        self.verifying_key = verifying_key
        self.store_full_blocks = store_full_blocks
        # self.all_masters = set(VKBook.get_masternodes()) - set(self.verifying_key)
        self.all_masters = set(VKBook.get_masternodes())
        # for block zero its going to just return 0x64 hash n 0 blk_num
        self.curr_hash, self.curr_num = StateDriver.get_latest_block_info()
        self.target_blk_num = -1
        # self.pending_block_updates = defaultdict(dict)

        self.run_catchup()

    def run_catchup(self):
        if self.catchup_state is True:
            self.log.critical("catch up already running we shouldn't be here")
            return

        # starting phase I
        self.catchup_state = self.send_block_idx_req()

    # Phase I start
    def send_block_idx_req(self):
        """
        Multi-casting BlockIndexRequests to all master nodes with current block hash
        :return:
        """
        self.log.info("Multi cast BlockIndexRequests to all MN with current block hash {}".format(curr_hash))

        req = BlockIndexRequest.create(block_hash=self.curr_hash)
        self.pub.send_msg(req, header=CATCHUP_MN_DN_FILTER.encode())
        return True

    # ONLY MASTERNODES USE THIS
    def recv_block_idx_req(self, requester_vk: str, request: BlockIndexRequest):
        """
        Receive BlockIndexRequests calls storage driver to process req and build response
        :param requester_vk:
        :param request:
        :return:
        """
        assert self.store_full_blocks, "Must be able to store full blocks to reply to state update requests"
        delta_idx = CatchupUtil.process_catch_up_idx(vk = requester_vk, curr_blk_hash = request.block_hash)
        self._send_block_idx_reply(catchup_list = delta_idx)