"""Calculate edge speeds, travel times, and weighted shortest paths."""

from __future__ import annotations

import itertools
import logging as lg
import multiprocessing as mp
import re
from collections.abc import Iterable
from collections.abc import Iterator
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import overload
from warnings import warn

import networkx as nx
import numpy as np
import pandas as pd

from . import convert
from . import utils

if TYPE_CHECKING:
    import geopandas as gpd


def route_to_gdf(
    G: nx.MultiDiGraph,
    route: list[int],
    *,
    weight: str = "length",
) -> gpd.GeoDataFrame:
    """
    Return a GeoDataFrame of the edges in a path, in order.

    Parameters
    ----------
    G
        Input graph.
    route
        Node IDs constituting the path.
    weight
        Attribute value to minimize when choosing between parallel edges.

    Returns
    -------
    gdf_edges
    """
    pairs = zip(route[:-1], route[1:])
    uvk = ((u, v, min(G[u][v].items(), key=lambda i: i[1][weight])[0]) for u, v in pairs)
    return convert.graph_to_gdfs(G.subgraph(route), nodes=False).loc[uvk]


# orig/dest int, weight present, cpus present
@overload
def shortest_path(
    G: nx.MultiDiGraph,
    orig: int,
    dest: int,
    *,
    weight: str,
    cpus: int | None,
) -> list[int] | None: ...


# orig/dest int, weight missing, cpus present
@overload
def shortest_path(
    G: nx.MultiDiGraph,
    orig: int,
    dest: int,
    *,
    cpus: int | None,
) -> list[int] | None: ...


# orig/dest int, weight present, cpus missing
@overload
def shortest_path(
    G: nx.MultiDiGraph,
    orig: int,
    dest: int,
    *,
    weight: str,
) -> list[int] | None: ...


# orig/dest int, weight missing, cpus missing
@overload
def shortest_path(
    G: nx.MultiDiGraph,
    orig: int,
    dest: int,
) -> list[int] | None: ...


# orig/dest Iterable, weight present, cpus present
@overload
def shortest_path(
    G: nx.MultiDiGraph,
    orig: Iterable[int],
    dest: Iterable[int],
    *,
    weight: str,
    cpus: int | None,
) -> list[list[int] | None]: ...


# orig/dest Iterable, weight missing, cpus present
@overload
def shortest_path(
    G: nx.MultiDiGraph,
    orig: Iterable[int],
    dest: Iterable[int],
    *,
    cpus: int | None,
) -> list[list[int] | None]: ...


# orig/dest Iterable, weight present, cpus missing
@overload
def shortest_path(
    G: nx.MultiDiGraph,
    orig: Iterable[int],
    dest: Iterable[int],
    *,
    weight: str,
) -> list[list[int] | None]: ...


# orig/dest Iterable, weight missing, cpus missing
@overload
def shortest_path(
    G: nx.MultiDiGraph,
    orig: Iterable[int],
    dest: Iterable[int],
) -> list[list[int] | None]: ...


def shortest_path(
    G: nx.MultiDiGraph,
    orig: int | Iterable[int],
    dest: int | Iterable[int],
    *,
    weight: str = "length",
    cpus: int | None = 1,
) -> list[int] | None | list[list[int] | None]:
    """
    Solve shortest path from origin node(s) to destination node(s).

    Uses Dijkstra's algorithm. If `orig` and `dest` are single node IDs, this
    will return a list of the nodes constituting the shortest path between
    them. If `orig` and `dest` are lists of node IDs, this will return a list
    of lists of the nodes constituting the shortest path between each
    origin-destination pair. If a path cannot be solved, this will return None
    for that path. You can parallelize solving multiple paths with the `cpus`
    parameter, but be careful to not exceed your available RAM.

    See also `k_shortest_paths` to solve multiple shortest paths between a
    single origin and destination. For additional functionality or different
    solver algorithms, use NetworkX directly.

    Parameters
    ----------
    G
        Input graph,
    orig
        Origin node ID(s).
    dest
        Destination node ID(s).
    weight
        Edge attribute to minimize when solving shortest path.
    cpus
        How many CPU cores to use. If None, use all available.

    Returns
    -------
    path
        The node IDs constituting the shortest path, or, if `orig` and `dest`
        are both iterable, then a list of such paths.
    """
    _verify_edge_attribute(G, weight)

    # if neither orig nor dest is iterable, just return the shortest path
    if not (isinstance(orig, Iterable) or isinstance(dest, Iterable)):
        return _single_shortest_path(G, orig, dest, weight)

    # if only 1 of orig or dest is iterable and the other is not, raise error
    if not (isinstance(orig, Iterable) and isinstance(dest, Iterable)):
        msg = "`orig` and `dest` must either both be iterable or neither must be iterable."
        raise TypeError(msg)

    # if both orig and dest are iterable, make them lists (so we're guaranteed
    # to be able to get their sizes) then ensure they have same lengths
    orig = list(orig)
    dest = list(dest)
    if len(orig) != len(dest):  # pragma: no cover
        msg = "`orig` and `dest` must be of equal length."
        raise ValueError(msg)

    # determine how many cpu cores to use
    if cpus is None:
        cpus = mp.cpu_count()
    cpus = min(cpus, mp.cpu_count())

    msg = f"Solving {len(orig)} paths with {cpus} CPUs..."
    utils.log(msg, level=lg.INFO)

    # if single-threading, calculate each shortest path one at a time
    if cpus == 1:
        paths = [_single_shortest_path(G, o, d, weight) for o, d in zip(orig, dest)]

    # if multi-threading, calculate shortest paths in parallel
    else:
        args = ((G, o, d, weight) for o, d in zip(orig, dest))
        with mp.get_context("spawn").Pool(cpus) as pool:
            paths = pool.starmap_async(_single_shortest_path, args).get()

    return paths


def k_shortest_paths(
    G: nx.MultiDiGraph,
    orig: int,
    dest: int,
    k: int,
    *,
    weight: str = "length",
) -> Iterator[list[int]]:
    """
    Solve `k` shortest paths from an origin node to a destination node.

    Uses Yen's algorithm. See also `shortest_path` to solve just the one
    shortest path.

    Parameters
    ----------
    G
        Input graph.
    orig
        Origin node ID.
    dest
        Destination node ID.
    k
        Number of shortest paths to solve.
    weight
        Edge attribute to minimize when solving shortest paths.

    Yields
    ------
    path
        The node IDs constituting the next-shortest path.
    """
    _verify_edge_attribute(G, weight)
    paths_gen = nx.shortest_simple_paths(
        G=convert.to_digraph(G, weight=weight),
        source=orig,
        target=dest,
        weight=weight,
    )
    yield from itertools.islice(paths_gen, 0, k)


def _single_shortest_path(
    G: nx.MultiDiGraph,
    orig: int,
    dest: int,
    weight: str,
) -> list[int] | None:
    """
    Solve the shortest path from an origin node to a destination node.

    This function uses Dijkstra's algorithm. It is a convenience wrapper
    around `networkx.shortest_path`, with exception handling for unsolvable
    paths. If the path is unsolvable, it returns None.

    Parameters
    ----------
    G
        Input graph.
    orig
        Origin node ID.
    dest
        Destination node ID.
    weight
        Edge attribute to minimize when solving shortest path.

    Returns
    -------
    path
        The node IDs constituting the shortest path.
    """
    try:
        return list(nx.shortest_path(G, orig, dest, weight=weight, method="dijkstra"))
    except nx.exception.NetworkXNoPath:  # pragma: no cover
        msg = f"Cannot solve path from {orig} to {dest}"
        utils.log(msg, level=lg.WARNING)
        return None


def _verify_edge_attribute(G: nx.MultiDiGraph, attr: str) -> None:
    """
    Verify attribute values are numeric and non-null across graph edges.

    Raises a ValueError if this attribute contains non-numeric values, and
    issues a UserWarning if this attribute is missing or null on any edges.

    Parameters
    ----------
    G
        Input graph.
    attr
        Name of the edge attribute to verify.

    Returns
    -------
    None
    """
    try:
        values_float = (np.array(tuple(G.edges(data=attr)))[:, 2]).astype(float)
        if np.isnan(values_float).any():
            msg = f"The attribute {attr!r} is missing or null on some edges."
            warn(msg, category=UserWarning, stacklevel=2)
    except ValueError as e:
        msg = f"The edge attribute {attr!r} contains non-numeric values."
        raise ValueError(msg) from e


def add_edge_speeds(
    G: nx.MultiDiGraph,
    *,
    hwy_speeds: dict[str, float] | None = None,
    fallback: float | None = None,
    agg: Callable[[Any], Any] = np.mean,
) -> nx.MultiDiGraph:
    """
    Add edge speeds (km per hour) to graph as new `speed_kph` edge attributes.

    By default, this imputes free-flow travel speeds for all edges via the
    mean `maxspeed` value of the edges of each highway type. For highway types
    in the graph that have no `maxspeed` value on any edge, it assigns the
    mean of all `maxspeed` values in graph.

    This default mean-imputation can obviously be imprecise, and the user can
    override it by passing in `hwy_speeds` and/or `fallback` arguments that
    correspond to local speed limit standards. The user can also specify a
    different aggregation function (such as the median) to impute missing
    values from the observed values.

    If edge `maxspeed` attribute has "mph" in it, value will automatically be
    converted from miles per hour to km per hour. Any other speed units should
    be manually converted to km per hour prior to running this function,
    otherwise there could be unexpected results. If "mph" does not appear in
    the edge's maxspeed attribute string, then function assumes kph, per OSM
    guidelines: https://wiki.openstreetmap.org/wiki/Map_Features/Units

    Parameters
    ----------
    G
        Input graph.
    hwy_speeds
        Dict keys are OSM highway types and values are typical speeds (km per
        hour) to assign to edges of that highway type for any edges missing
        speed data. Any edges with highway type not in `hwy_speeds` will be
        assigned the mean pre-existing speed value of all edges of that
        highway type.
    fallback
        Default speed value (km per hour) to assign to edges whose highway
        type did not appear in `hwy_speeds` and had no pre-existing speed
        attribute values on any edge.
    agg
        Aggregation function to impute missing values from observed values.
        The default is `numpy.mean`, but you might also consider for example
        `numpy.median`, `numpy.nanmedian`, or your own custom function.

    Returns
    -------
    G
        Graph with `speed_kph` attributes on all edges.
    """
    if fallback is None:
        fallback = np.nan

    edges = convert.graph_to_gdfs(G, nodes=False, fill_edge_geometry=False)

    # collapse any highway lists (can happen during graph simplification)
    # into string values simply by keeping just the first element of the list
    edges["highway"] = edges["highway"].map(lambda x: x[0] if isinstance(x, list) else x)

    if "maxspeed" in edges.columns:
        # collapse any maxspeed lists (can happen during graph simplification)
        # into a single value
        edges["maxspeed"] = edges["maxspeed"].apply(_collapse_multiple_maxspeed_values, agg=agg)

        # create speed_kph by cleaning maxspeed strings and converting mph to
        # kph if necessary
        edges["speed_kph"] = edges["maxspeed"].astype(str).map(_clean_maxspeed).astype(float)
    else:
        # if no edges in graph had a maxspeed attribute
        edges["speed_kph"] = None

    # if user provided hwy_speeds, use them as default values, otherwise
    # initialize an empty series to populate with values
    hwy_speed_avg = pd.Series(dtype=float) if hwy_speeds is None else pd.Series(hwy_speeds).dropna()

    # for each highway type that caller did not provide in hwy_speeds, impute
    # speed of type by taking the mean of the preexisting speed values of that
    # highway type
    for hwy, group in edges.groupby("highway"):
        if hwy not in hwy_speed_avg:
            hwy_speed_avg.loc[hwy] = agg(group["speed_kph"])

    # if any highway types had no preexisting speed values, impute their speed
    # with fallback value provided by caller. if fallback=np.nan, impute speed
    # as the mean speed of all highway types that did have preexisting values
    hwy_speed_avg = hwy_speed_avg.fillna(fallback).fillna(agg(hwy_speed_avg))

    # for each edge missing speed data, assign it the imputed value for its
    # highway type
    speed_kph = (
        edges[["highway", "speed_kph"]].set_index("highway").iloc[:, 0].fillna(hwy_speed_avg)
    )

    # all speeds will be null if edges had no preexisting maxspeed data and
    # caller did not pass in hwy_speeds or fallback arguments
    if pd.isna(speed_kph).all():
        msg = (
            "This graph's edges have no preexisting 'maxspeed' attribute "
            "values so you must pass `hwy_speeds` or `fallback` arguments."
        )
        raise ValueError(msg)

    # add speed kph attribute to graph edges
    edges["speed_kph"] = speed_kph.to_numpy()
    nx.set_edge_attributes(G, values=edges["speed_kph"], name="speed_kph")

    return G


def add_edge_travel_times(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """
    Add edge travel time (seconds) to graph as new `travel_time` edge attributes.

    Calculates free-flow travel time along each edge, based on `length` and
    `speed_kph` attributes. Note: run `add_edge_speeds` first to generate the
    `speed_kph` attribute. All edges must have `length` and `speed_kph`
    attributes and all their values must be non-null.

    Parameters
    ----------
    G
        Input graph.

    Returns
    -------
    G
        Graph with `travel_time` attributes on all edges.
    """
    edges = convert.graph_to_gdfs(G, nodes=False)

    # verify edge length and speed_kph attributes exist
    if not ("length" in edges.columns and "speed_kph" in edges.columns):  # pragma: no cover
        msg = "All edges must have 'length' and 'speed_kph' attributes."
        raise KeyError(msg)

    # verify edge length and speed_kph attributes contain no nulls
    if pd.isna(edges["length"]).any() or pd.isna(edges["speed_kph"]).any():  # pragma: no cover
        msg = "Edge 'length' and 'speed_kph' values must be non-null."
        raise ValueError(msg)

    # convert distance meters to km, and speed km per hour to km per second
    distance_km = edges["length"] / 1000
    speed_km_sec = edges["speed_kph"] / (60 * 60)

    # calculate edge travel time in seconds
    travel_time = distance_km / speed_km_sec

    # add travel time attribute to graph edges
    edges["travel_time"] = travel_time.to_numpy()
    nx.set_edge_attributes(G, values=edges["travel_time"], name="travel_time")

    return G


def _clean_maxspeed(
    maxspeed: str | float,
    *,
    agg: Callable[[Any], Any] = np.mean,
    convert_mph: bool = True,
) -> float | None:
    """
    Clean a maxspeed string and convert mph to kph if necessary.

    If present, splits maxspeed on "|" (which denotes that the value contains
    different speeds per lane) then aggregates the resulting values. Invalid
    inputs return None. See https://wiki.openstreetmap.org/wiki/Key:maxspeed
    for details on values and formats.

    Parameters
    ----------
    maxspeed
        An OSM way "maxspeed" attribute value. Null values are expected to be
        of type float (`numpy.nan`), and non-null values are strings.
    agg
        Aggregation function if `maxspeed` contains multiple values (default
        is `numpy.mean`).
    convert_mph
        If True, convert miles per hour to kilometers per hour.

    Returns
    -------
    clean_value
        Clean value resulting from `agg` function.
    """
    MILES_TO_KM = 1.60934
    if not isinstance(maxspeed, str):
        return None

    # regex adapted from OSM wiki
    pattern = "^([0-9][\\.,0-9]+?)(?:[ ]?(?:km/h|kmh|kph|mph|knots))?$"
    values = re.split(r"\|", maxspeed)  # creates a list even if it's a single value
    try:
        clean_values = []
        for value in values:
            match = re.match(pattern, value)
            clean_value = float(match.group(1).replace(",", "."))  # type: ignore[union-attr]
            if convert_mph and "mph" in maxspeed.lower():
                clean_value = clean_value * MILES_TO_KM
            clean_values.append(clean_value)
        return float(agg(clean_values))

    except (ValueError, AttributeError):
        # if invalid input, return None
        return None


def _collapse_multiple_maxspeed_values(
    value: str | float | list[str | float],
    agg: Callable[[Any], Any],
) -> float | str | None:
    """
    Collapse a list of maxspeed values to a single value.

    Returns None if a ValueError is encountered.

    Parameters
    ----------
    value
        An OSM way "maxspeed" attribute value. Null values are expected to be
        of type float (`numpy.nan`), and non-null values are strings.
    agg
        The aggregation function to reduce the list to a single value.

    Returns
    -------
    collapsed
        If `value` was a string or null, it is just returned directly.
        Otherwise, the return is a float representation of the aggregated
        value in the list (converted to kph if original value was in mph).
    """
    # if this isn't a list, just return it right back to the caller
    if not isinstance(value, list):
        return value

    # otherwise, if it is a list, process it
    try:
        # clean each value in list and convert to kph if it is mph then
        # return a single aggregated value
        values = [_clean_maxspeed(x) for x in value]
        collapsed = float(agg(pd.Series(values).dropna()))
        if pd.isna(collapsed):
            return None
        # otherwise
        return collapsed  # noqa: TRY300
    except ValueError:
        return None
