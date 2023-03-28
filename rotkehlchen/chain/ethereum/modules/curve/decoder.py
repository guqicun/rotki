import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Optional
from rotkehlchen.accounting.structures.evm_event import EvmProduct

from rotkehlchen.accounting.structures.types import HistoryEventSubType, HistoryEventType
from rotkehlchen.assets.asset import Asset
from rotkehlchen.chain.ethereum.utils import asset_normalized_value
from rotkehlchen.chain.evm.constants import ETH_SPECIAL_ADDRESS, ZERO_ADDRESS
from rotkehlchen.chain.evm.decoding.interfaces import DecoderInterface, ReloadableDecoderMixin
from rotkehlchen.chain.evm.decoding.structures import (
    DEFAULT_DECODING_OUTPUT,
    DEFAULT_ENRICHMENT_OUTPUT,
    DecoderContext,
    DecodingOutput,
    EnricherContext,
    TransferEnrichmentOutput,
)
from rotkehlchen.chain.evm.decoding.utils import maybe_reshuffle_events
from rotkehlchen.chain.evm.structures import EvmTxReceiptLog
from rotkehlchen.chain.evm.types import string_to_evm_address
from rotkehlchen.constants.assets import A_ETH
from rotkehlchen.constants.resolver import evm_address_to_identifier
from rotkehlchen.errors.asset import UnknownAsset, WrongAssetType
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.types import ChainID, ChecksumEvmAddress, EvmTokenKind, EvmTransaction
from rotkehlchen.utils.misc import hex_or_bytes_to_address, hex_or_bytes_to_int

from .constants import CPT_CURVE
from .curve_cache import read_curve_pools_and_gauges

if TYPE_CHECKING:
    from rotkehlchen.accounting.structures.evm_event import EvmEvent
    from rotkehlchen.chain.ethereum.node_inquirer import EthereumInquirer
    from rotkehlchen.chain.evm.decoding.base import BaseDecoderTools
    from rotkehlchen.user_messages import MessagesAggregator


ADD_LIQUIDITY = b'B?d\x95\xa0\x8f\xc6RB\\\xf4\xed\r\x1f\x9e7\xe5q\xd9\xb9R\x9b\x1c\x1c#\xcc\xe7\x80\xb2\xe7\xdf\r'  # noqa: E501
ADD_LIQUIDITY_2_ASSETS = b'&\xf5Z\x85\x08\x1d$\x97N\x85\xc6\xc0\x00E\xd0\xf0E9\x91\xe9Xs\xf5+\xff\r!\xaf@y\xa7h'  # noqa: E501
ADD_LIQUIDITY_4_ASSETS = b'?\x19\x15w^\x0c\x9a8\xa5z{\xb7\xf1\xf9\x00_Ho\xb9\x04\xe1\xf8J\xa2\x156MVs\x19\xa5\x8d'  # noqa: E501
REMOVE_LIQUIDITY = b"Z\xd0V\xf2\xe2\x8a\x8c\xec# \x15@k\x846h\xc1\xe3l\xdaY\x81'\xec;\x8cY\xb8\xc7's\xa0"  # noqa: E501
REMOVE_ONE = b'\x9e\x96\xdd;\x99z*%~\xecM\xf9\xbbn\xafbn m\xf5\xf5C\xbd\x966\x82\xd1C0\x0b\xe3\x10'  # noqa: E501
REMOVE_LIQUIDITY_3_ASSETS = b'\xa4\x9dL\xf0&V\xae\xbf\x8cw\x1fZ\x85\x85c\x8a*\x15\xeel\x97\xcfr\x05\xd4 \x8e\xd7\xc1\xdf%-'  # noqa: E501
REMOVE_LIQUIDITY_4_ASSETS = b'\x98x\xca7^\x10o*C\xc3\xb5\x99\xfcbEh\x13\x1cL\x9aK\xa6j\x14V7\x15v;\xe9\xd5\x9d'  # noqa: E501
REMOVE_LIQUIDITY_IMBALANCE = b'\xb9d\xb7/s\xf5\xef[\xf0\xfd\xc5Y\xb2\xfa\xb9\xa7\xb1*9\xe4x\x17\xa5G\xf1\xf0\xae\xe4\x7f\xeb\xd6\x02'  # noqa: E501
CURVE_Y_DEPOSIT = string_to_evm_address('0xbBC81d23Ea2c3ec7e56D39296F0cbB648873a5d3')

GAUGE_DEPOSIT = b'\xe1\xff\xfc\xc4\x92=\x04\xb5Y\xf4\xd2\x9a\x8b\xfcl\xda\x04\xeb[\r<F\x07Q\xc2@,\\\\\xc9\x10\x9c'  # noqa: E501
GAUGE_WITHDRAW = b'\x88N\xda\xd9\xceo\xa2D\r\x8aT\xcc\x124\x90\xeb\x96\xd2v\x84y\xd4\x9f\xf9\xc76a%\xa9BCd'  # noqa: E501

TOKEN_EXCHANGE = b'\x8b>\x96\xf2\xb8\x89\xfaw\x1cS\xc9\x81\xb4\r\xaf\x00_c\xf67\xf1\x86\x9fppR\xd1Z=\xd9q@'  # noqa: E501
TOKEN_EXCHANGE_UNDERLYING = b'\xd0\x13\xca#\xe7ze\x00<,e\x9cTB\xc0\x0c\x80Sq\xb7\xfc\x1e\xbdL lA\xd1Sk\xd9\x0b'  # noqa: E501
EXCHANGE_MULTIPLE = b'\x14\xb5a\x17\x8a\xe0\xf3h\xf4\x0f\xaf\xd0H\\Oq)\xeaq\xcd\xc0\x0bL\xe1\xe5\x94\x0f\x9b\xc6Y\xc8\xb2'  # noqa: E501
CURVE_SWAP_ROUTER = string_to_evm_address('0x99a58482BD75cbab83b27EC03CA68fF489b5788f')

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


def _read_curve_asset(
        asset_address: Optional[ChecksumEvmAddress],
        chain_id: ChainID,
) -> Optional[Asset]:
    """
    A thin wrapper that turns asset address into an asset object.

    Object returned here is a pure Asset (not a resolved CryptoAsset) since it is meant only for
    comparison with other assets. And to compare with other assets there is no need to resolve.
    """
    if asset_address is None:
        return None

    if asset_address == ETH_SPECIAL_ADDRESS:
        return A_ETH

    return Asset(evm_address_to_identifier(
        address=asset_address,
        chain_id=chain_id,
        token_type=EvmTokenKind.ERC20,
    ))


class CurveDecoder(DecoderInterface, ReloadableDecoderMixin):

    def __init__(
            self,
            ethereum_inquirer: 'EthereumInquirer',  # pylint: disable=unused-argument
            base_tools: 'BaseDecoderTools',
            msg_aggregator: 'MessagesAggregator',
    ) -> None:
        super().__init__(
            evm_inquirer=ethereum_inquirer,
            base_tools=base_tools,
            msg_aggregator=msg_aggregator,
        )
        self.curve_pools, self.curve_gauges = read_curve_pools_and_gauges()
        self.ethereum = ethereum_inquirer

    def _decode_curve_remove_events(
            self,
            transaction: EvmTransaction,
            tx_log: EvmTxReceiptLog,
            decoded_events: list['EvmEvent'],
            user_address: ChecksumEvmAddress,
    ) -> DecodingOutput:
        """Decode information related to withdrawing assets from curve pools"""
        withdrawal_events: list['EvmEvent'] = []
        return_event: Optional['EvmEvent'] = None
        for event in decoded_events:
            try:
                crypto_asset = event.asset.resolve_to_crypto_asset()
            except (UnknownAsset, WrongAssetType):
                self.notify_user(event=event, counterparty=CPT_CURVE)
                continue

            if (  # Withdraw eth
                event.event_type == HistoryEventType.RECEIVE and
                event.event_subtype == HistoryEventSubType.NONE and
                event.asset == A_ETH and
                event.location_label == user_address
            ):
                event.event_type = HistoryEventType.WITHDRAWAL
                event.event_subtype = HistoryEventSubType.REMOVE_ASSET
                event.counterparty = CPT_CURVE
                event.notes = f'Remove {event.balance.amount} {crypto_asset.symbol} from the curve pool'  # noqa: E501
                withdrawal_events.append(event)
            elif (  # Withdraw send wrapped
                event.event_type == HistoryEventType.SPEND and
                event.event_subtype == HistoryEventSubType.NONE and
                event.location_label == transaction.from_address and
                (
                    user_address == event.location_label or
                    tx_log.topics[0] == REMOVE_LIQUIDITY_IMBALANCE
                )
            ):
                event.event_type = HistoryEventType.SPEND
                event.event_subtype = HistoryEventSubType.RETURN_WRAPPED
                event.counterparty = CPT_CURVE
                event.notes = f'Return {event.balance.amount} {crypto_asset.symbol}'
                return_event = event
            elif (  # Withdraw receive asset
                event.event_type == HistoryEventType.RECEIVE and
                event.event_subtype == HistoryEventSubType.NONE and
                event.location_label == transaction.from_address and
                user_address == event.location_label and
                tx_log.address in self.curve_pools
            ):
                event.event_type = HistoryEventType.WITHDRAWAL
                event.event_subtype = HistoryEventSubType.REMOVE_ASSET
                event.counterparty = CPT_CURVE
                event.notes = f'Remove {event.balance.amount} {crypto_asset.symbol} from the curve pool {tx_log.address}'  # noqa: E501
                withdrawal_events.append(event)

        # Make sure that the order is the following:
        # 1. Return pool token event
        # 2. Withdrawal 1
        # 3. Withdrawal 2
        # etc.
        if return_event is None or len(withdrawal_events) == 0:
            log.debug(
                f'Expected to see a receive pool token event and deposit events for a curve pool, '
                f'but have not found them. Tx_hash: {transaction.tx_hash.hex()} '
                f'User address: {user_address}',
            )
            return DEFAULT_DECODING_OUTPUT

        return_event.extra_data = {'withdrawal_events_num': len(withdrawal_events)}  # for accounting  # noqa: E501
        previous_event = return_event
        for event in withdrawal_events:
            maybe_reshuffle_events(previous_event, event, decoded_events)
            previous_event = event
        return DEFAULT_DECODING_OUTPUT

    def _decode_curve_deposit_events(
            self,
            transaction: EvmTransaction,
            tx_log: EvmTxReceiptLog,
            decoded_events: list['EvmEvent'],
            user_address: ChecksumEvmAddress,
    ) -> DecodingOutput:
        """Decode information related to depositing assets in curve pools"""
        deposit_events: list['EvmEvent'] = []
        receive_event: Optional['EvmEvent'] = None
        for event in decoded_events:
            try:
                crypto_asset = event.asset.resolve_to_crypto_asset()
            except (UnknownAsset, WrongAssetType):
                self.notify_user(event=event, counterparty=CPT_CURVE)
                continue

            if (  # Deposit ETH
                event.event_type == HistoryEventType.SPEND and
                event.event_subtype == HistoryEventSubType.NONE and
                event.asset == A_ETH and
                event.location_label == user_address
            ):
                event.event_type = HistoryEventType.DEPOSIT
                event.event_subtype = HistoryEventSubType.DEPOSIT_ASSET
                event.counterparty = CPT_CURVE
                event.notes = f'Deposit {event.balance.amount} {crypto_asset.symbol} in curve pool'  # noqa: E501
                deposit_events.append(event)
            elif (  # deposit give asset
                (
                    event.event_type == HistoryEventType.SPEND and
                    event.event_subtype == HistoryEventSubType.NONE and
                    event.location_label == user_address and
                    tx_log.address in self.curve_pools
                ) or
                (
                    tx_log.topics[0] == ADD_LIQUIDITY_4_ASSETS and
                    user_address == CURVE_Y_DEPOSIT and
                    event.asset != A_ETH
                )
            ):
                event.event_type = HistoryEventType.DEPOSIT
                event.event_subtype = HistoryEventSubType.DEPOSIT_ASSET
                event.counterparty = CPT_CURVE
                event.notes = f'Deposit {event.balance.amount} {crypto_asset.symbol} in curve pool'  # noqa: E501
                if tx_log.address in self.curve_pools:
                    event.notes += f' {tx_log.address}'
                deposit_events.append(event)
            elif (  # Deposit receive pool token
                event.event_type == HistoryEventType.RECEIVE and
                event.event_subtype == HistoryEventSubType.NONE and
                event.location_label == user_address and
                tx_log.address in self.curve_pools
            ):
                event.event_type = HistoryEventType.RECEIVE
                event.event_subtype = HistoryEventSubType.RECEIVE_WRAPPED
                event.counterparty = CPT_CURVE
                event.notes = f'Receive {event.balance.amount} {crypto_asset.symbol} after depositing in curve pool {tx_log.address}'  # noqa: E501
                receive_event = event
            elif (  # deposit give asset
                event.event_type == HistoryEventType.SPEND and
                event.event_subtype == HistoryEventSubType.NONE and
                event.location_label == user_address and
                tx_log.address in self.curve_pools
            ):
                event.event_type = HistoryEventType.DEPOSIT
                event.event_subtype = HistoryEventSubType.DEPOSIT_ASSET
                event.counterparty = CPT_CURVE
                event.notes = f'Deposit {event.balance.amount} {crypto_asset.symbol} in curve pool {tx_log.address}'  # noqa: E501
                deposit_events.append(event)

        # Make sure that the order is the following:
        # 1. Receive pool token event
        # 2. Deposit 1
        # 3. Deposit 2
        # etc.
        if receive_event is None or len(deposit_events) == 0:
            log.debug(
                f'Expected to see a receive pool token event and deposit events for a curve pool, '
                f'but have not found them. Tx_hash: {transaction.tx_hash.hex()} '
                f'User address: {user_address}',
            )
            return DEFAULT_DECODING_OUTPUT

        receive_event.extra_data = {'deposit_events_num': len(deposit_events)}  # for accounting
        previous_event = receive_event
        for event in deposit_events:
            maybe_reshuffle_events(previous_event, event, decoded_events)
            previous_event = event
        return DEFAULT_DECODING_OUTPUT

    def _decode_curve_trades(self, context: DecoderContext) -> DecodingOutput:
        """Decode curve trades made via single pools or curve swap router
        First determine:
        - `spender_address`
        - `receiver_address`
        - `sold_token_address`
        - `bought_token_address`
        - `raw_sold_amount`
        - `raw_bought_amount`

        Then create assets if `sold_token_address` and `bought_token_address` were found.
        Then match and label events.
        Then reshuffle events to make sure that spend and receive are consecutive.

        Note that `sold_token_address` and `bought_token_address` are not always found (e.g.
        when pool for some reason is not present in our cache). If tokens that were swapped are
        detected then we use them when iterating over `decoded_events` list and matching transfers.
        If they are not detected then conditions when matching transfer events are a bit broader.
        """

        # These are nullable because in case a curve pool is not stored in our cache or if it
        # is a swap in a metapool (TOKEN_EXCHANGE_UNDERLYING) we will skip token check.
        sold_token_address: Optional[ChecksumEvmAddress] = None
        bought_token_address: Optional[ChecksumEvmAddress] = None

        swapping_contract: ChecksumEvmAddress
        if context.tx_log.topics[0] in (TOKEN_EXCHANGE, TOKEN_EXCHANGE_UNDERLYING):
            pool_address = context.tx_log.address
            swapping_contract = pool_address
            # When a single pool is used, spender and receiver is always the same
            spender_address = receiver_address = hex_or_bytes_to_address(context.tx_log.topics[1])
            sold_token_id = hex_or_bytes_to_int(context.tx_log.data[:32])
            raw_sold_amount = hex_or_bytes_to_int(context.tx_log.data[32:64])
            bought_token_id = hex_or_bytes_to_int(context.tx_log.data[64:96])
            raw_bought_amount = hex_or_bytes_to_int(context.tx_log.data[96:128])
            if (
                context.tx_log.topics[0] == TOKEN_EXCHANGE and
                pool_address in self.curve_pools and
                len(self.curve_pools[pool_address]) > max(sold_token_id, bought_token_id)  # Make sure that tokens of the pool are cached  # noqa: E501
            ):
                sold_token_address = self.curve_pools[pool_address][sold_token_id]
                bought_token_address = self.curve_pools[pool_address][bought_token_id]
        else:  # EXCHANGE_MULTIPLE
            swapping_contract = CURVE_SWAP_ROUTER
            spender_address = hex_or_bytes_to_address(context.tx_log.topics[1])
            receiver_address = hex_or_bytes_to_address(context.tx_log.topics[2])
            raw_sold_amount = hex_or_bytes_to_int(context.tx_log.data[-64:-32])
            raw_bought_amount = hex_or_bytes_to_int(context.tx_log.data[-32:])
            # Curve swap router logs route (a list of addresses) that was used. Route consists of
            # 9 elements. Consider X a number of pools that was used. Then the structure can be
            # described in the following way:
            # At 0 index: Address of the sold token (token that goes in the router)
            # From 1 to X indices: Addresses of pools that were used
            # At X + 1 index: Address of the bought token (token that comes from the router)
            # From X + 2 to 8 indices: Unused elements (zero addresses)
            # Here we read only addresses of token in and token out.
            sold_token_address = hex_or_bytes_to_address(context.tx_log.data[:32])
            for i in range(1, 9):  # Starting from 1 because at 0 is `sold_token_address`
                address = hex_or_bytes_to_address(context.tx_log.data[32 * i:32 * (i + 1)])
                if address == ZERO_ADDRESS:
                    break
                bought_token_address = address

        sold_asset = _read_curve_asset(sold_token_address, self.evm_inquirer.chain_id)
        bought_asset = _read_curve_asset(bought_token_address, self.evm_inquirer.chain_id)
        spend_event: Optional['EvmEvent'] = None
        receive_event: Optional['EvmEvent'] = None
        for event in context.decoded_events:
            if event.address != swapping_contract:
                continue

            crypto_asset = event.asset.resolve_to_crypto_asset()
            if (
                event.location_label == spender_address and
                event.event_type == HistoryEventType.SPEND and
                event.balance.amount == asset_normalized_value(amount=raw_sold_amount, asset=crypto_asset) and  # noqa: E501
                (sold_asset is None or event.asset == sold_asset)
            ):
                event.event_type = HistoryEventType.TRADE
                event.event_subtype = HistoryEventSubType.SPEND
                event.notes = f'Swap {event.balance.amount} {crypto_asset.symbol} in curve'
                event.counterparty = CPT_CURVE
                spend_event = event
            elif (
                event.location_label == receiver_address and
                event.event_type == HistoryEventType.RECEIVE and
                event.balance.amount == asset_normalized_value(amount=raw_bought_amount, asset=crypto_asset) and  # noqa: E501
                (bought_asset is None or event.asset == bought_asset)
            ):
                event.event_type = HistoryEventType.TRADE
                event.event_subtype = HistoryEventSubType.RECEIVE
                event.notes = f'Receive {event.balance.amount} {crypto_asset.symbol} as the result of a swap in curve'  # noqa: E501
                event.counterparty = CPT_CURVE
                receive_event = event

        if spend_event is not None and receive_event is not None:
            # Just to make sure that spend and receive events are consecutive
            maybe_reshuffle_events(spend_event, receive_event, context.decoded_events)
        else:
            log.error(f'Did not find spend and receive events for a curve swap. {spend_event=} {receive_event=}')  # noqa: E501

        return DEFAULT_DECODING_OUTPUT

    def _decode_curve_events(self, context: DecoderContext) -> DecodingOutput:
        if context.tx_log.topics[0] in (
            REMOVE_LIQUIDITY,
            REMOVE_ONE,
            REMOVE_LIQUIDITY_IMBALANCE,
            REMOVE_LIQUIDITY_3_ASSETS,
            REMOVE_LIQUIDITY_4_ASSETS,
        ):
            user_address = hex_or_bytes_to_address(context.tx_log.topics[1])
            return self._decode_curve_remove_events(
                tx_log=context.tx_log,
                transaction=context.transaction,
                decoded_events=context.decoded_events,
                user_address=user_address,
            )
        if context.tx_log.topics[0] in (
            ADD_LIQUIDITY,
            ADD_LIQUIDITY_2_ASSETS,
            ADD_LIQUIDITY_4_ASSETS,
        ):
            user_address = hex_or_bytes_to_address(context.tx_log.topics[1])
            return self._decode_curve_deposit_events(
                transaction=context.transaction,
                tx_log=context.tx_log,
                decoded_events=context.decoded_events,
                user_address=user_address,
            )

        if context.tx_log.topics[0] in (
            TOKEN_EXCHANGE,
            TOKEN_EXCHANGE_UNDERLYING,
            EXCHANGE_MULTIPLE,
        ):
            return self._decode_curve_trades(context=context)

        return DEFAULT_DECODING_OUTPUT

    def _decode_curve_gauge_events(self, context: DecoderContext) -> DecodingOutput:
        if context.tx_log.topics[0] not in (GAUGE_DEPOSIT, GAUGE_WITHDRAW):
            return DEFAULT_DECODING_OUTPUT

        provider = hex_or_bytes_to_address(context.tx_log.topics[1])
        gauge_address = context.tx_log.address
        raw_amount = hex_or_bytes_to_int(context.tx_log.data)
        for event in context.decoded_events:
            crypto_asset = event.asset.resolve_to_crypto_asset()
            if (
                event.location_label == provider and
                event.address == gauge_address and
                event.balance.amount == asset_normalized_value(amount=raw_amount, asset=crypto_asset)  # noqa: E501
            ):
                event.counterparty = CPT_CURVE
                event.product = EvmProduct.CURVE_GAUGE
                if context.tx_log.topics[0] == GAUGE_DEPOSIT:
                    event.event_type = HistoryEventType.DEPOSIT
                    event.event_subtype = HistoryEventSubType.DEPOSIT_ASSET
                    event.notes = f'Deposit {event.balance.amount} {crypto_asset.symbol} into {gauge_address} curve gauge'  # noqa: E501
                else:  # Withdraw
                    event.event_type = HistoryEventType.WITHDRAWAL
                    event.event_subtype = HistoryEventSubType.REMOVE_ASSET
                    event.notes = f'Withdraw {event.balance.amount} {crypto_asset.symbol} from {gauge_address} curve gauge'  # noqa: E501

        return DEFAULT_DECODING_OUTPUT

    def _maybe_enrich_curve_transfers(self, context: EnricherContext) -> TransferEnrichmentOutput:
        """
        May raise:
        - UnknownAsset
        - WrongAssetType
        """
        source_address = hex_or_bytes_to_address(context.tx_log.topics[1])
        to_address = hex_or_bytes_to_address(context.tx_log.topics[2])
        if (  # deposit give asset
            context.event.event_type == HistoryEventType.RECEIVE and
            context.event.event_subtype == HistoryEventSubType.NONE and
            source_address == CURVE_Y_DEPOSIT and
            context.transaction.from_address == to_address
        ):
            crypto_asset = context.event.asset.resolve_to_crypto_asset()
            context.event.event_type = HistoryEventType.WITHDRAWAL
            context.event.event_subtype = HistoryEventSubType.REMOVE_ASSET
            context.event.counterparty = CPT_CURVE
            context.event.notes = f'Receive {context.event.balance.amount} {crypto_asset.symbol} from the curve pool {CURVE_Y_DEPOSIT}'  # noqa: E501
            return DEFAULT_ENRICHMENT_OUTPUT
        if (
            context.event.event_type == HistoryEventType.RECEIVE and
            context.event.event_subtype == HistoryEventSubType.NONE and
            source_address in self.curve_gauges
        ):
            crypto_asset = context.event.asset.resolve_to_crypto_asset()
            context.event.event_subtype = HistoryEventSubType.REWARD
            context.event.notes = f'Receive {context.event.balance.amount} {crypto_asset.symbol} rewards from {source_address} curve gauge'  # noqa: E501
            context.event.counterparty = CPT_CURVE
        return DEFAULT_ENRICHMENT_OUTPUT

    # -- DecoderInterface methods

    def addresses_to_decoders(self) -> dict[ChecksumEvmAddress, tuple[Any, ...]]:
        mapping: dict[ChecksumEvmAddress, tuple[Any, ...]] = {
            address: (self._decode_curve_events,)
            for address in self.curve_pools
        }
        mapping.update({  # addresses of pools and gauges don't intersect, so combining like this is fine  # noqa: E501
            gauge_address: (self._decode_curve_gauge_events,)
            for gauge_address in self.curve_gauges
        })
        mapping[CURVE_SWAP_ROUTER] = (self._decode_curve_events,)
        return mapping

    def enricher_rules(self) -> list[Callable]:
        return [
            self._maybe_enrich_curve_transfers,
        ]

    def counterparties(self) -> list[str]:
        return [CPT_CURVE]

    def reload_data(self) -> Optional[Mapping[ChecksumEvmAddress, tuple[Any, ...]]]:
        """Make sure curve pools are recently queried from the chain, saved in the DB
        and loaded to the decoder's memory.

        If a query happens and any new mappings are generated they are returned,
        otherwise `None` is returned.
        """
        self.ethereum.assure_curve_protocol_cache_is_queried()
        new_curve_pools, new_curve_gauges = read_curve_pools_and_gauges()
        curve_pools_diff = set(new_curve_pools.keys()) - set(self.curve_pools.keys())
        curve_gauges_diff = new_curve_gauges - self.curve_gauges
        if len(curve_pools_diff) == 0 and len(curve_gauges_diff) == 0:
            return None

        self.curve_pools = new_curve_pools
        self.curve_gauges = new_curve_gauges
        new_mapping: dict[ChecksumEvmAddress, tuple[Any, ...]] = {
            pool_address: (self._decode_curve_events,)
            for pool_address in curve_pools_diff
        }
        new_mapping.update({  # addresses of pools and gauges don't intersect, so combining like this is fine  # noqa: E501
            gauge_address: (self._decode_curve_gauge_events,)
            for gauge_address in curve_gauges_diff
        })
        return new_mapping
