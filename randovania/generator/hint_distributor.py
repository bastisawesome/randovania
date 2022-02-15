import copy
import dataclasses
from abc import ABC
from random import Random
from typing import Iterator, Union, Optional, Callable, Dict

from randovania.game_description import node_search
from randovania.game_description.game_description import GameDescription
from randovania.game_description.game_patches import GamePatches
from randovania.game_description.hint import (
    Hint, HintType, PrecisionPair, HintLocationPrecision, HintItemPrecision,
    HintRelativeAreaName, RelativeDataArea, RelativeDataItem
)
from randovania.game_description.resources.logbook_asset import LogbookAsset
from randovania.game_description.resources.pickup_index import PickupIndex
from randovania.game_description.world.area import Area
from randovania.game_description.world.node import PickupNode, LogbookNode, LoreType
from randovania.game_description.world.node_identifier import NodeIdentifier
from randovania.game_description.world.world_list import WorldList
from randovania.generator.filler.filler_library import should_have_hint
from randovania.generator.filler.player_state import PlayerState
from randovania.generator.filler.runner import PlayerPool
from randovania.layout.base.base_configuration import BaseConfiguration
from randovania.lib import random_lib
from randovania.resolver import debug

HintProvider = Callable[[PlayerState, GamePatches, Random, PickupIndex], Optional[Hint]]


def _not_empty(it: Iterator) -> bool:
    return sum(1 for _ in it) > 0


HintTargetPrecision = tuple[PickupIndex, PrecisionPair]


@dataclasses.dataclass(frozen=True)
class PreFillParams:
    rng: Random
    configuration: BaseConfiguration
    game: GameDescription
    is_multiworld: bool


class HintDistributor(ABC):
    @property
    def num_joke_hints(self) -> int:
        return 0

    def get_generic_logbook_nodes(self, prefill: PreFillParams):
        return [node
                for node in prefill.game.world_list.all_nodes
                if isinstance(node, LogbookNode)
                and node.lore_type.holds_generic_hint]

    async def get_specific_pickup_precision_pair_overrides(self, patches: GamePatches, prefill: PreFillParams
                                                           ) -> dict[NodeIdentifier, PrecisionPair]:
        return {}

    async def assign_specific_location_hints(self, patches: GamePatches, prefill: PreFillParams) -> GamePatches:
        specific_location_precisions = await self.get_specific_pickup_precision_pair_overrides(patches, prefill)

        # TODO: this is an Echoes default. Should not have a default and all nodes have one in the DB.
        default_precision = PrecisionPair(HintLocationPrecision.KEYBEARER, HintItemPrecision.BROAD_CATEGORY,
                                          include_owner=True)

        wl = prefill.game.world_list
        for node in wl.all_nodes:
            if isinstance(node, LogbookNode) and node.lore_type == LoreType.SPECIFIC_PICKUP:
                patches = patches.assign_hint(
                    node.resource(),
                    Hint(HintType.LOCATION,
                         specific_location_precisions.get(wl.identifier_for_node(node), default_precision),
                         PickupIndex(node.hint_index))
                )

        return patches

    async def get_guranteed_hints(self, patches: GamePatches, prefill: PreFillParams) -> list[HintTargetPrecision]:
        return []

    async def assign_guaranteed_indices_hints(self, patches: GamePatches, nodes: list[LogbookNode],
                                              prefill: PreFillParams) -> GamePatches:
        # Specific Pickup/any LogbookNode Hints
        indices_with_hint = await self.get_guranteed_hints(patches, prefill)
        prefill.rng.shuffle(indices_with_hint)

        all_logbook_nodes = [node for node in nodes if node.resource() not in patches.hints]
        prefill.rng.shuffle(all_logbook_nodes)

        for index, precision in indices_with_hint:
            if not all_logbook_nodes:
                break

            logbook_node = all_logbook_nodes.pop()
            patches = patches.assign_hint(logbook_node.resource(), Hint(HintType.LOCATION, precision, index))
            nodes.remove(logbook_node)

        return patches

    async def assign_other_hints(self, patches: GamePatches, nodes: list[LogbookNode],
                                 prefill: PreFillParams) -> GamePatches:
        return patches

    async def assign_joke_hints(self, patches: GamePatches, nodes: list[LogbookNode],
                                prefill: PreFillParams) -> GamePatches:

        all_logbook_nodes = [node for node in nodes if node.resource() not in patches.hints]
        prefill.rng.shuffle(all_logbook_nodes)

        num_joke = self.num_joke_hints

        while num_joke > 0 and all_logbook_nodes:
            logbook_asset = (node := all_logbook_nodes.pop()).resource()
            patches = patches.assign_hint(logbook_asset, Hint(HintType.JOKE, None))
            num_joke -= 1
            nodes.remove(node)

        return patches

    async def assign_pre_filler_hints(self, patches: GamePatches, prefill: PreFillParams) -> GamePatches:
        patches = await self.assign_specific_location_hints(patches, prefill)
        logbook_nodes = self.get_generic_logbook_nodes(prefill)
        prefill.rng.shuffle(logbook_nodes)
        patches = await self.assign_guaranteed_indices_hints(patches, logbook_nodes, prefill)
        patches = await self.assign_other_hints(patches, logbook_nodes, prefill)
        patches = await self.assign_joke_hints(patches, logbook_nodes, prefill)
        return patches

    async def assign_post_filler_hints(self, patches: GamePatches, rng: Random,
                                       player_pool: PlayerPool, player_state: PlayerState,
                                       ) -> GamePatches:
        # Since we haven't added expansions yet, these hints will always be for items added by the filler.
        full_hints_patches = self.fill_unassigned_hints(
            patches, player_state.game.world_list, rng,
            player_state.scan_asset_initial_pickups,
        )
        return await self.assign_precision_to_hints(full_hints_patches, rng, player_pool, player_state)

    async def assign_precision_to_hints(self, patches: GamePatches, rng: Random,
                                        player_pool: PlayerPool, player_state: PlayerState) -> GamePatches:
        """
        Ensures no hints present in `patches` has no precision.
        :param patches:
        :param rng:
        :param player_pool:
        :param player_state:
        :return:
        """
        raise NotImplementedError()

    def fill_unassigned_hints(self, patches: GamePatches,
                              world_list: WorldList,
                              rng: Random,
                              scan_asset_initial_pickups: dict[LogbookAsset, frozenset[PickupIndex]],
                              ) -> GamePatches:
        new_hints = copy.copy(patches.hints)

        # Get all LogbookAssets from the WorldList
        potential_hint_locations: set[LogbookAsset] = {
            node.resource()
            for node in world_list.all_nodes
            if isinstance(node, LogbookNode)
        }
        for logbook in potential_hint_locations:
            if logbook not in scan_asset_initial_pickups:
                scan_asset_initial_pickups[logbook] = frozenset()

        # But remove these that already have hints
        potential_hint_locations -= patches.hints.keys()

        # Get interesting items to place hints for
        possible_indices = set(patches.pickup_assignment.keys())
        possible_indices -= {hint.target for hint in patches.hints.values() if hint.target is not None}
        possible_indices -= {index for index in possible_indices
                             if not should_have_hint(patches.pickup_assignment[index].pickup.item_category)}

        debug.debug_print("fill_unassigned_hints had {} decent indices for {} hint locations".format(
            len(possible_indices), len(potential_hint_locations)))

        if debug.debug_level() > 1:
            print(f"> Num pickups per asset:")
            for asset, pickups in scan_asset_initial_pickups.items():
                print(f"* {asset}: {len(pickups)} pickups")
            print("> Done.")

        # But if we don't have enough hints, just pick randomly from everything
        if len(possible_indices) < len(potential_hint_locations):
            possible_indices = {node.pickup_index
                                for node in world_list.all_nodes
                                if isinstance(node, PickupNode)}

        # Get an stable order
        ordered_possible_indices = list(sorted(possible_indices))
        ordered_potential_hint_locations = list(sorted(potential_hint_locations))

        num_logbooks: dict[PickupIndex, int] = {
            index: sum(1 for indices in scan_asset_initial_pickups.values() if index in indices)
            for index in ordered_possible_indices
        }
        max_seen = max(num_logbooks.values())
        pickup_indices_weight: dict[PickupIndex, int] = {
            index: max_seen - num_logbook
            for index, num_logbook in num_logbooks.items()
        }
        # Ensure all indices are present with at least weight 0
        for index in ordered_possible_indices:
            if index not in pickup_indices_weight:
                pickup_indices_weight[index] = 0

        for logbook in sorted(ordered_potential_hint_locations,
                              key=lambda r: len(scan_asset_initial_pickups[r]),
                              reverse=True):
            try:
                new_index = random_lib.select_element_with_weight(pickup_indices_weight, rng)
            except StopIteration:
                # If everything has weight 0, then just choose randomly.
                new_index = random_lib.random_key(pickup_indices_weight, rng)

            del pickup_indices_weight[new_index]

            new_hints[logbook] = Hint(HintType.LOCATION, None, new_index)
            debug.debug_print(f"Added hint at {logbook} for item at {new_index}")

        return dataclasses.replace(patches, hints=new_hints)

    def replace_hints_without_precision_with_jokes(self, patches: GamePatches) -> GamePatches:
        """
        Adds WRONG_GAME precision to all hints that are missing one precision.
        :param patches:
        :return:
        """

        hints_to_replace = {
            asset: Hint(HintType.JOKE, None)
            for asset, hint in patches.hints.items()
            if hint.precision is None and hint.hint_type == HintType.LOCATION
        }

        return dataclasses.replace(patches, hints={
            asset: hints_to_replace.get(asset, hint)
            for asset, hint in patches.hints.items()
        })

    def precision_pair_weighted_list(self) -> list[PrecisionPair]:
        raise NotImplementedError()

    def add_relative_hint(self, world_list: WorldList,
                          patches: GamePatches,
                          rng: Random,
                          target: PickupIndex,
                          target_precision: HintItemPrecision,
                          relative_type: HintLocationPrecision,
                          precise_distance: bool,
                          precision: Union[HintItemPrecision, HintRelativeAreaName],
                          max_distance: int,
                          ) -> Optional[Hint]:
        """
        Creates a relative hint.
        :return: Might be None, if no hint could be created.
        """
        target_node = node_search.pickup_index_to_node(world_list, target)
        target_area = world_list.nodes_to_area(target_node)
        distances = node_search.distances_to_node(world_list, target_node, patches=patches, cutoff=max_distance)

        def _major_pickups(area: Area) -> Iterator[PickupIndex]:
            for index in area.pickup_indices:
                t = patches.pickup_assignment.get(index)
                # FIXME: None should be ok, but this must be called after junk has been filled
                if t is not None:
                    cat = t.pickup.item_category
                    if cat.is_major or (not cat.is_expansion and target_precision == HintItemPrecision.DETAILED):
                        yield index

        area_choices = {
            area: 1 / max(distance, 2)
            for area, distance in distances.items()
            if (distance > 0 and area.in_dark_aether == target_area.in_dark_aether
                and (relative_type == HintLocationPrecision.RELATIVE_TO_AREA or _not_empty(_major_pickups(area))))
        }
        if not area_choices:
            return None

        area = random_lib.select_element_with_weight(dict(sorted(area_choices.items(),
                                                                 key=lambda a: a[0].name)), rng)

        distance_offset = None
        if not precise_distance:
            distance_offset = max_distance - distances[area]

        if relative_type == HintLocationPrecision.RELATIVE_TO_AREA:
            relative = RelativeDataArea(distance_offset, world_list.identifier_for_area(area),
                                        precision)
        elif relative_type == HintLocationPrecision.RELATIVE_TO_INDEX:
            relative = RelativeDataItem(distance_offset, rng.choice(list(_major_pickups(area))), precision)
        else:
            raise ValueError(f"Invalid relative_type: {relative_type}")

        precision_pair = PrecisionPair(relative_type, target_precision, include_owner=False, relative=relative)
        return Hint(HintType.LOCATION, precision_pair, target)

    def _relative(self,
                  relative_type: HintLocationPrecision,
                  precise_distance: bool,
                  precision: Union[HintItemPrecision, HintRelativeAreaName],
                  max_distance: int,
                  ) -> HintProvider:
        def _wrapper(player_state: PlayerState, patches: GamePatches, rng: Random, target: PickupIndex):
            return self.add_relative_hint(player_state.game.world_list, patches, rng, target,
                                          HintItemPrecision.DETAILED, relative_type,
                                          precise_distance, precision, max_distance)

        return _wrapper

    def _get_relative_hint_providers(self) -> list[HintProvider]:
        raise NotImplementedError()

    def add_hints_precision(self,
                            player_state: PlayerState,
                            patches: GamePatches,
                            rng: Random,
                            ) -> GamePatches:
        """
        Adds precision to all hints that are missing one.
        :param player_state:
        :param patches:
        :param rng:
        :return:
        """

        hints_to_replace: Dict[LogbookAsset, Hint] = {
            asset: hint
            for asset, hint in patches.hints.items()
            if hint.precision is None and hint.hint_type == HintType.LOCATION
        }

        relative_hint_providers = self._get_relative_hint_providers()
        asset_ids = list(hints_to_replace.keys())
        rng.shuffle(asset_ids)

        while asset_ids and relative_hint_providers:
            new_hint = relative_hint_providers.pop()(player_state, patches, rng, hints_to_replace[asset_ids[-1]].target)
            if new_hint is not None:
                hints_to_replace[asset_ids.pop()] = new_hint

        # Add random precisions
        precisions = []
        for asset_id in asset_ids:
            precisions = random_lib.create_weighted_list(rng, precisions, self.precision_pair_weighted_list)
            precision = precisions.pop()

            hints_to_replace[asset_id] = dataclasses.replace(hints_to_replace[asset_id], precision=precision)

        # Replace the hints the in the patches
        return dataclasses.replace(patches, hints={
            asset: hints_to_replace.get(asset, hint)
            for asset, hint in patches.hints.items()
        })


class AllJokesHintDistributor(HintDistributor):
    async def assign_precision_to_hints(self, patches: GamePatches, rng: Random,
                                        player_pool: PlayerPool, player_state: PlayerState) -> GamePatches:
        return self.replace_hints_without_precision_with_jokes(patches)