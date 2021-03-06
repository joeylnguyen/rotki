import logging
import random
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union, cast
from urllib.parse import urlparse

import requests
from ens import ENS
from ens.abis import ENS as ENS_ABI, RESOLVER as ENS_RESOLVER_ABI
from ens.main import ENS_MAINNET_ADDR
from ens.utils import is_none_or_zero_address, normal_name_to_hash, normalize_name
from eth_typing import BlockNumber
from eth_utils.address import to_checksum_address
from typing_extensions import Literal
from web3 import HTTPProvider, Web3
from web3._utils.abi import get_abi_output_types
from web3._utils.contracts import find_matching_event_abi
from web3._utils.filters import construct_event_filter_params
from web3.datastructures import MutableAttributeDict
from web3.middleware.exception_retry_request import http_retry_request_middleware

from rotkehlchen.chain.ethereum.transactions import EthTransactions
from rotkehlchen.constants.ethereum import ETH_SCAN
from rotkehlchen.db.dbhandler import DBHandler
from rotkehlchen.errors import BlockchainQueryError, RemoteError, UnableToDecryptRemoteData
from rotkehlchen.externalapis.etherscan import Etherscan
from rotkehlchen.fval import FVal
from rotkehlchen.greenlets import GreenletManager
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.serialization.serialize import process_result
from rotkehlchen.typing import ChecksumEthAddress, Timestamp
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.misc import from_wei, hex_or_bytes_to_str, request_get_dict

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)

DEFAULT_ETH_RPC_TIMEOUT = 10


def _is_synchronized(current_block: int, latest_block: int) -> Tuple[bool, str]:
    """ Validate that the ethereum node is synchronized
            within 20 blocks of latest block

        Returns a tuple (results, message)
            - result: Boolean for confirmation of synchronized
            - message: A message containing information on what the status is.
    """
    message = ''
    if current_block < (latest_block - 20):
        message = (
            f'Found ethereum node but it is out of sync. {current_block} / '
            f'{latest_block}. Will use etherscan.'
        )
        log.warning(message)
        return False, message

    return True, message


class NodeName(Enum):
    OWN = 0
    ETHERSCAN = 1
    MYCRYPTO = 2
    BLOCKSCOUT = 3
    AVADO_POOL = 4

    def __str__(self) -> str:
        if self == NodeName.OWN:
            return 'own node'
        elif self == NodeName.ETHERSCAN:
            return 'etherscan'
        elif self == NodeName.MYCRYPTO:
            return 'mycrypto'
        elif self == NodeName.BLOCKSCOUT:
            return 'blockscout'
        elif self == NodeName.AVADO_POOL:
            return 'avado pool'

        raise RuntimeError(f'Corrupt value {self} for NodeName -- Should never happen')

    def endpoint(self, own_rpc_endpoint: str) -> str:
        if self == NodeName.OWN:
            return own_rpc_endpoint
        elif self == NodeName.ETHERSCAN:
            raise TypeError('Called endpoint for etherscan')
        elif self == NodeName.MYCRYPTO:
            return 'https://api.mycryptoapi.com/eth'
        elif self == NodeName.BLOCKSCOUT:
            return 'https://mainnet-nethermind.blockscout.com/'
        elif self == NodeName.AVADO_POOL:
            return 'https://mainnet.eth.cloud.ava.do/'

        raise RuntimeError(f'Corrupt value {self} for NodeName -- Should never happen')


OPEN_NODES = (
    NodeName.MYCRYPTO,
    NodeName.BLOCKSCOUT,
    NodeName.AVADO_POOL,
    NodeName.ETHERSCAN,
)
ETHEREUM_NODES_TO_CONNECT_AT_START = (
    NodeName.OWN,
    NodeName.MYCRYPTO,
    NodeName.BLOCKSCOUT,
    NodeName.AVADO_POOL,
)
OPEN_NODES_WEIGHT_MAP = {  # Probability with which to select each node
    NodeName.ETHERSCAN: 0.5,
    NodeName.MYCRYPTO: 0.25,
    NodeName.BLOCKSCOUT: 0.2,
    NodeName.AVADO_POOL: 0.05,
}


class EthereumManager():
    def __init__(
            self,
            ethrpc_endpoint: str,
            etherscan: Etherscan,
            database: DBHandler,
            msg_aggregator: MessagesAggregator,
            greenlet_manager: GreenletManager,
            connect_at_start: Sequence[NodeName],
            eth_rpc_timeout: int = DEFAULT_ETH_RPC_TIMEOUT,
    ) -> None:
        log.debug(f'Initializing Ethereum Manager with {ethrpc_endpoint}')
        self.greenlet_manager = greenlet_manager
        self.web3_mapping: Dict[NodeName, Web3] = {}
        self.own_rpc_endpoint = ethrpc_endpoint
        self.etherscan = etherscan
        self.msg_aggregator = msg_aggregator
        self.eth_rpc_timeout = eth_rpc_timeout
        self.transactions = EthTransactions(
            database=database,
            etherscan=etherscan,
            msg_aggregator=msg_aggregator,
        )
        for node in connect_at_start:
            self.greenlet_manager.spawn_and_track(
                after_seconds=None,
                task_name=f'Attempt connection to {str(node)} ethereum node',
                method=self.attempt_connect,
                name=node,
                ethrpc_endpoint=node.endpoint(self.own_rpc_endpoint),
                mainnet_check=True,
            )

    def connected_to_any_web3(self) -> bool:
        return (
            NodeName.OWN in self.web3_mapping or
            NodeName.MYCRYPTO in self.web3_mapping or
            NodeName.BLOCKSCOUT in self.web3_mapping or
            NodeName.AVADO_POOL in self.web3_mapping
        )

    def default_call_order(self) -> Sequence[NodeName]:
        """Default call order for ethereum nodes

        Own node always has preference. Then all other node types are randomly queried
        in sequence depending on a weighted probability.


        Some benchmarks on weighted probability based random selection when compared
        to simple random selection. Benchmark was on blockchain balance querying with
        29 ethereum accounts and at the time 1010 different ethereum tokens.

        With weights: etherscan: 0.5, mycrypto: 0.25, blockscout: 0.2, avado: 0.05
        ===> Runs: 66, 58, 60, 68, 58 seconds
        ---> Average: 62 seconds
        - Without weights
        ===> Runs: 66, 82, 72, 58, 72 seconds
        ---> Average: 70 seconds
        """
        result = []
        if NodeName.OWN in self.web3_mapping:
            result.append(NodeName.OWN)

        selection = list(OPEN_NODES)
        ordered_list = []
        while len(selection) != 0:
            weights = []
            for entry in selection:
                weights.append(OPEN_NODES_WEIGHT_MAP[entry])
            node = random.choices(selection, weights, k=1)
            ordered_list.append(node[0])
            selection.remove(node[0])

        return result + ordered_list

    def attempt_connect(
            self,
            name: NodeName,
            ethrpc_endpoint: str,
            mainnet_check: bool = True,
    ) -> Tuple[bool, str]:
        """Attempt to connect to a particular node type

        For our own node if the given rpc endpoint is not the same as the saved one
        the connection is re-attempted to the new one
        """
        message = ''
        node_connected = self.web3_mapping.get(name, None) is not None
        own_node_already_connected = (
            name == NodeName.OWN and
            self.own_rpc_endpoint == ethrpc_endpoint and
            node_connected
        )
        if own_node_already_connected or (node_connected and name != NodeName.OWN):
            return True, 'Already connected to an ethereum node'

        try:
            parsed_eth_rpc_endpoint = urlparse(ethrpc_endpoint)
            if not parsed_eth_rpc_endpoint.scheme:
                ethrpc_endpoint = f"http://{ethrpc_endpoint}"
            provider = HTTPProvider(
                endpoint_uri=ethrpc_endpoint,
                request_kwargs={'timeout': self.eth_rpc_timeout},
            )
            ens = ENS(provider)
            web3 = Web3(provider, ens=ens)
            web3.middleware_onion.inject(http_retry_request_middleware, layer=0)
        except requests.exceptions.ConnectionError:
            message = f'Failed to connect to ethereum node {name} at endpoint {ethrpc_endpoint}'
            log.warning(message)
            return False, message

        if web3.isConnected():
            # Also make sure we are actually connected to the Ethereum mainnet
            synchronized = True
            msg = ''
            if mainnet_check:
                network_id = int(web3.net.version)
                if network_id != 1:
                    message = (
                        f'Connected to ethereum node {name} at endpoint {ethrpc_endpoint} but '
                        f'it is not on the ethereum mainnet. The chain id '
                        f'the node is in is {network_id}.'
                    )
                    log.warning(message)
                    return False, message

                current_block = web3.eth.blockNumber  # pylint: disable=no-member
                try:
                    latest_block = self.query_eth_highest_block()
                except RemoteError:
                    msg = 'Could not query latest block'
                    log.warning(msg)
                    synchronized = False
                else:
                    synchronized, msg = _is_synchronized(current_block, latest_block)

            if not synchronized:
                self.msg_aggregator.add_warning(
                    f'We could not verify that ethereum node {name} is '
                    'synchronized with the ethereum mainnet. Balances and other queries '
                    'may be incorrect.',
                )

            log.info(f'Connected ethereum node {name} at {ethrpc_endpoint}')
            self.web3_mapping[name] = web3
            return True, ''
        else:
            message = f'Failed to connect to ethereum node {name} at endpoint {ethrpc_endpoint}'
            log.warning(message)

        # If we get here we did not connnect
        return False, message

    def set_rpc_endpoint(self, endpoint: str) -> Tuple[bool, str]:
        """ Attempts to set the RPC endpoint for the user's own ethereum node

           Returns a tuple (result, message)
               - result: Boolean for success or failure of changing the rpc endpoint
               - message: A message containing information on what happened. Can
                          be populated both in case of success or failure"""
        if endpoint == '':
            self.web3_mapping.pop(NodeName.OWN, None)
            self.own_rpc_endpoint = ''
            return True, ''
        else:
            result, message = self.attempt_connect(name=NodeName.OWN, ethrpc_endpoint=endpoint)
            if result:
                log.info('Setting own node ETH RPC endpoint', endpoint=endpoint)
                self.own_rpc_endpoint = endpoint
            return result, message

    def query(self, method: Callable, call_order: Sequence[NodeName], **kwargs: Any) -> Any:
        """Queries ethereum related data by performing the provided method to all given nodes

        The first node in the call order that gets a succcesful response returns.
        If none get a result then a remote error is raised
        """
        for node in call_order:
            web3 = self.web3_mapping.get(node, None)
            if web3 is None and node != NodeName.ETHERSCAN:
                continue

            try:
                result = method(web3, **kwargs)
            except (RemoteError, BlockchainQueryError, requests.exceptions.HTTPError) as e:
                log.warning(f'Failed to query {node} for {str(method)} due to {str(e)}')
                # Catch all possible errors here and just try next node call
                continue

            return result

        # no node in the call order list was succesfully queried
        raise RemoteError(
            f'Failed to query {str(method)} after trying the following '
            f'nodes: {[str(x) for x in call_order]}',
        )

    def _get_latest_block_number(self, web3: Optional[Web3]) -> int:
        if web3 is not None:
            return web3.eth.blockNumber

        # else
        return self.etherscan.get_latest_block_number()

    def get_latest_block_number(self, call_order: Optional[Sequence[NodeName]] = None) -> int:
        return self.query(
            method=self._get_latest_block_number,
            call_order=call_order if call_order is not None else self.default_call_order(),
        )

    def query_eth_highest_block(self) -> BlockNumber:
        """ Attempts to query an external service for the block height

        Returns the highest blockNumber

        May Raise RemoteError if querying fails
        """

        url = 'https://api.blockcypher.com/v1/eth/main'
        log.debug('Querying blockcypher for ETH highest block', url=url)
        eth_resp: Optional[Dict[str, str]]
        try:
            eth_resp = request_get_dict(url)
        except (RemoteError, UnableToDecryptRemoteData, requests.exceptions.ReadTimeout):
            eth_resp = None

        block_number: Optional[int]
        if eth_resp and 'height' in eth_resp:
            block_number = int(eth_resp['height'])
            log.debug('ETH highest block result', block=block_number)
        else:
            block_number = self.etherscan.get_latest_block_number()
            log.debug('ETH highest block result', block=block_number)

        return BlockNumber(block_number)

    def get_eth_balance(self, account: ChecksumEthAddress) -> FVal:
        """Gets the balance of the given account in ETH

        May raise:
        - RemoteError if Etherscan is used and there is a problem querying it or
        parsing its response
        """
        result = self.get_multieth_balance([account])
        return result[account]

    def get_multieth_balance(
            self,
            accounts: List[ChecksumEthAddress],
            call_order: Optional[Sequence[NodeName]] = None,
    ) -> Dict[ChecksumEthAddress, FVal]:
        """Returns a dict with keys being accounts and balances in ETH

        May raise:
        - RemoteError if an external service such as Etherscan is queried and
          there is a problem with its query.
        """
        balances: Dict[ChecksumEthAddress, FVal] = {}
        log.debug(
            'Querying ethereum chain for ETH balance',
            eth_addresses=accounts,
        )
        result = self.call_contract(
            contract_address=ETH_SCAN.address,
            abi=ETH_SCAN.abi,
            method_name='etherBalances',
            arguments=[accounts],
            call_order=call_order if call_order is not None else self.default_call_order(),
        )
        balances = {}
        for idx, account in enumerate(accounts):
            balances[account] = from_wei(result[idx])
        return balances

    def get_block_by_number(
            self,
            num: int,
            call_order: Optional[Sequence[NodeName]] = None,
    ) -> Dict[str, Any]:
        return self.query(
            method=self._get_block_by_number,
            call_order=call_order if call_order is not None else self.default_call_order(),
            num=num,
        )

    def _get_block_by_number(self, web3: Optional[Web3], num: int) -> Dict[str, Any]:
        """Returns the block object corresponding to the given block number

        May raise:
        - RemoteError if an external service such as Etherscan is queried and
        there is a problem with its query.
        """
        if web3 is None:
            return self.etherscan.get_block_by_number(num)

        block_data: MutableAttributeDict = MutableAttributeDict(web3.eth.getBlock(num))  # type: ignore # pylint: disable=no-member  # noqa: E501
        block_data['hash'] = hex_or_bytes_to_str(block_data['hash'])
        return block_data  # type: ignore

    def get_code(
            self,
            account: ChecksumEthAddress,
            call_order: Optional[Sequence[NodeName]] = None,
    ) -> str:
        return self.query(
            method=self._get_code,
            call_order=call_order if call_order is not None else self.default_call_order(),
            account=account,
        )

    def _get_code(self, web3: Optional[Web3], account: ChecksumEthAddress) -> str:
        """Gets the deployment bytecode at the given address

        May raise:
        - RemoteError if Etherscan is used and there is a problem querying it or
        parsing its response
        """
        if web3 is None:
            return self.etherscan.get_code(account)

        return hex_or_bytes_to_str(web3.eth.getCode(account))

    def ens_lookup(
            self,
            name: str,
            call_order: Optional[Sequence[NodeName]] = None,
    ) -> Optional[ChecksumEthAddress]:
        return self.query(
            method=self._ens_lookup,
            call_order=call_order if call_order is not None else self.default_call_order(),
            name=name,
        )

    def _ens_lookup(self, web3: Optional[Web3], name: str) -> Optional[ChecksumEthAddress]:
        """Performs an ENS lookup and returns address if found else None

        May raise:
        - RemoteError if Etherscan is used and there is a problem querying it or
        parsing its response
        """
        if web3 is not None:
            return web3.ens.resolve(name)

        # else we gotta manually query contracts via etherscan
        normal_name = normalize_name(name)
        resolver_addr = self._call_contract_etherscan(
            ENS_MAINNET_ADDR,
            abi=ENS_ABI,
            method_name='resolver',
            arguments=[normal_name_to_hash(normal_name)],
        )
        if is_none_or_zero_address(resolver_addr):
            return None
        address = self._call_contract_etherscan(
            to_checksum_address(resolver_addr),
            abi=ENS_RESOLVER_ABI,
            method_name='addr',
            arguments=[normal_name_to_hash(normal_name)],
        )

        if is_none_or_zero_address(address):
            return None
        return to_checksum_address(address)

    def _call_contract_etherscan(
            self,
            contract_address: ChecksumEthAddress,
            abi: List,
            method_name: str,
            arguments: Optional[List[Any]] = None,
    ) -> Any:
        """Performs an eth_call to an ethereum contract via etherscan

        May raise:
        - RemoteError if there is a problem with
        reaching etherscan or with the returned result
        """
        web3 = Web3()
        contract = web3.eth.contract(address=contract_address, abi=abi)
        input_data = contract.encodeABI(method_name, args=arguments if arguments else [])
        result = self.etherscan.eth_call(
            to_address=contract_address,
            input_data=input_data,
        )
        if result == '0x':
            raise BlockchainQueryError(
                f'Error doing call on contract {contract_address} via etherscan.'
                f' Returned 0x result',
            )

        fn_abi = contract._find_matching_fn_abi(
            fn_identifier=method_name,
            args=arguments,
        )
        output_types = get_abi_output_types(fn_abi)
        output_data = web3.codec.decode_abi(output_types, bytes.fromhex(result[2:]))

        if len(output_data) == 1:
            return output_data[0]
        return output_data

    def _get_transaction_receipt(
            self,
            web3: Optional[Web3],
            tx_hash: str,
    ) -> Dict[str, Any]:
        if web3 is None:
            tx_receipt = self.etherscan.get_transaction_receipt(tx_hash)
            try:
                # Turn hex numbers to int
                block_number = int(tx_receipt['blockNumber'], 16)
                tx_receipt['blockNumber'] = block_number
                tx_receipt['cumulativeGasUsed'] = int(tx_receipt['cumulativeGasUsed'], 16)
                tx_receipt['gasUsed'] = int(tx_receipt['gasUsed'], 16)
                tx_receipt['status'] = int(tx_receipt['status'], 16)
                tx_index = int(tx_receipt['transactionIndex'], 16)
                tx_receipt['transactionIndex'] = tx_index
                for log in tx_receipt['logs']:
                    log['blockNumber'] = block_number
                    log['logIndex'] = int(log['logIndex'], 16)
                    log['transactionIndex'] = tx_index
            except ValueError:
                raise RemoteError(
                    f'Couldnt deserialize transaction receipt data from etherscan {tx_receipt}',
                )
            return tx_receipt

        tx_receipt = web3.eth.getTransactionReceipt(tx_hash)  # type: ignore
        return process_result(tx_receipt)

    def get_transaction_receipt(
            self,
            tx_hash: str,
            call_order: Optional[Sequence[NodeName]] = None,
    ) -> Dict[str, Any]:
        return self.query(
            method=self._get_transaction_receipt,
            call_order=call_order if call_order is not None else self.default_call_order(),
            tx_hash=tx_hash,
        )

    def call_contract(
            self,
            contract_address: ChecksumEthAddress,
            abi: List,
            method_name: str,
            arguments: Optional[List[Any]] = None,
            call_order: Optional[Sequence[NodeName]] = None,
    ) -> Any:
        return self.query(
            method=self._call_contract,
            call_order=call_order if call_order is not None else self.default_call_order(),
            contract_address=contract_address,
            abi=abi,
            method_name=method_name,
            arguments=arguments,
        )

    def _call_contract(
            self,
            web3: Optional[Web3],
            contract_address: ChecksumEthAddress,
            abi: List,
            method_name: str,
            arguments: Optional[List[Any]] = None,
    ) -> Any:
        """Performs an eth_call to an ethereum contract

        May raise:
        - RemoteError if etherscan is used and there is a problem with
        reaching it or with the returned result
        - BlockchainQueryError if web3 is used and there is a VM execution error
        """
        if web3 is None:
            return self._call_contract_etherscan(
                contract_address=contract_address,
                abi=abi,
                method_name=method_name,
                arguments=arguments,
            )

        contract = web3.eth.contract(address=contract_address, abi=abi)
        try:
            method = getattr(contract.caller, method_name)
            result = method(*arguments if arguments else [])
        except ValueError as e:
            raise BlockchainQueryError(
                f'Error doing call on contract {contract_address}: {str(e)}',
            )
        return result

    def get_logs(
            self,
            contract_address: ChecksumEthAddress,
            abi: List,
            event_name: str,
            argument_filters: Dict[str, Any],
            from_block: int,
            to_block: Union[int, Literal['latest']] = 'latest',
            call_order: Sequence[NodeName] = (NodeName.OWN, NodeName.ETHERSCAN),
    ) -> List[Dict[str, Any]]:
        return self.query(
            method=self._get_logs,
            call_order=call_order,
            contract_address=contract_address,
            abi=abi,
            event_name=event_name,
            argument_filters=argument_filters,
            from_block=from_block,
            to_block=to_block,
        )

    def _get_logs(
            self,
            web3: Optional[Web3],
            contract_address: ChecksumEthAddress,
            abi: List,
            event_name: str,
            argument_filters: Dict[str, Any],
            from_block: int,
            to_block: Union[int, Literal['latest']] = 'latest',
    ) -> List[Dict[str, Any]]:
        """Queries logs of an ethereum contract

        May raise:
        - RemoteError if etherscan is used and there is a problem with
        reaching it or with the returned result
        """
        event_abi = find_matching_event_abi(abi=abi, event_name=event_name)
        _, filter_args = construct_event_filter_params(
            event_abi=event_abi,
            abi_codec=Web3().codec,
            contract_address=contract_address,
            argument_filters=argument_filters,
            fromBlock=from_block,
            toBlock=to_block,
        )
        if event_abi['anonymous']:
            # web3.py does not handle the anonymous events correctly and adds the first topic
            filter_args['topics'] = filter_args['topics'][1:]
        events: List[Dict[str, Any]] = []
        start_block = from_block
        if web3 is not None:
            until_block = web3.eth.blockNumber if to_block == 'latest' else to_block
            while start_block <= until_block:
                filter_args['fromBlock'] = start_block
                end_block = min(start_block + 250000, until_block)
                filter_args['toBlock'] = end_block
                log.debug(
                    'Querying node for contract event',
                    contract_address=contract_address,
                    event_name=event_name,
                    argument_filters=argument_filters,
                    from_block=filter_args['fromBlock'],
                    to_block=filter_args['toBlock'],
                )
                # WTF: for some reason the first time we get in here the loop resets
                # to the start without querying eth_getLogs and ends up with double logging
                new_events_web3 = cast(List[Dict[str, Any]], web3.eth.getLogs(filter_args))
                # Turn all HexBytes into hex strings
                for e_idx, event in enumerate(new_events_web3):
                    new_events_web3[e_idx]['blockHash'] = event['blockHash'].hex()
                    new_topics = []
                    for topic in (event['topics']):
                        new_topics.append(topic.hex())
                    new_events_web3[e_idx]['topics'] = new_topics
                    new_events_web3[e_idx]['transactionHash'] = event['transactionHash'].hex()

                start_block = end_block + 1
                events.extend(new_events_web3)
        else:  # etherscan
            until_block = (
                self.etherscan.get_latest_block_number() if to_block == 'latest' else to_block
            )
            while start_block <= until_block:
                end_block = min(start_block + 300000, until_block)
                new_events = self.etherscan.get_logs(
                    contract_address=contract_address,
                    topics=filter_args['topics'],  # type: ignore
                    from_block=start_block,
                    to_block=end_block,
                )
                # Turn all Hex ints to ints
                for e_idx, event in enumerate(new_events):
                    new_events[e_idx]['address'] = to_checksum_address(event['address'])
                    new_events[e_idx]['blockNumber'] = int(event['blockNumber'], 16)
                    new_events[e_idx]['timeStamp'] = int(event['timeStamp'], 16)
                    new_events[e_idx]['gasPrice'] = int(event['gasPrice'], 16)
                    new_events[e_idx]['gasUsed'] = int(event['gasUsed'], 16)
                    new_events[e_idx]['logIndex'] = int(event['logIndex'], 16)
                    new_events[e_idx]['transactionIndex'] = int(event['transactionIndex'], 16)

                start_block = end_block + 1
                events.extend(new_events)

        return events

    def get_event_timestamp(self, event: Dict[str, Any]) -> Timestamp:
        """Reads an event returned either by etherscan or web3 and gets its timestamp

        Etherscan events contain a timestamp. Normal web3 events don't so it needs to
        be queried from the block number

        WE could also add this to the get_logs() call but would add unnecessary
        rpc calls for get_block_by_number() for each log entry. Better have it
        lazy queried like this.

        TODO: Perhaps better approach would be a log event class for this
        """
        if 'timeStamp' in event:
            # event from etherscan
            return Timestamp(event['timeStamp'])

        # event from web3
        block_number = event['blockNumber']
        block_data = self.get_block_by_number(block_number)
        return Timestamp(block_data['timestamp'])
