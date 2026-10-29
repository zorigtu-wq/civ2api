"""
Road / railroad network between our cities (player request: "link the cities with railroad").

plan(tiles, cities, W2, start=None) -> {"edges": [...], "tiles": [(x, y), ...]}
  * candidate links: every pair of own cities on the same land mass at most `max_dist` apart
  * link cost: cheapest land path between the two city tiles, cost per tile by how long a
    road takes there (RULES.TXT road turns: Grassland/Plains/Desert 2, Forest/Hills/Tundra/
    Jungle/Swamp 4, Mountains 6; tiles that already have a road are free)
  * minimum spanning tree (Kruskal) -> the fewest road tiles that connect every city
  * tiles are ordered breadth-first from `start` (wonder cities first), so the busy caravan
    routes are built first
"""
from __future__ import annotations

import heapq
from collections import deque

COST = {"Grassland": 2, "Plains": 2, "Desert": 2, "Forest": 4, "Hills": 4, "Tundra": 4,
        "Jungle": 4, "Swamp": 4, "Mountains": 6, "Glacier": 8}
STEPS = ((1, -1), (2, 0), (1, 1), (0, 2), (-1, 1), (-2, 0), (-1, -1), (0, -2))


def _dist(a, b, W2):
    dx = abs(a[0] - b[0])
    dx = min(dx, W2 - dx)
    dy = abs(a[1] - b[1])
    return max(dy, (dx + dy) // 2)


def _path(T, a, b, W2, limit):
    """Cheapest land path a -> b (both city tiles), staying within `limit` of a."""
    best = {a: 0}
    prev = {}
    pq = [(0, a)]
    while pq:
        c, cur = heapq.heappop(pq)
        if cur == b:
            break
        if c > best.get(cur, 1e9):
            continue
        for dx, dy in STEPS:
            n = ((cur[0] + dx) % W2, cur[1] + dy)
            t = T.get(n)
            if not t or t["terrain"] == "Ocean" or _dist(a, n, W2) > limit:
                continue
            step = 0 if (t.get("road") or t.get("city") or n == b) else COST.get(t["terrain"], 4)
            nc = c + step + 0.01                       # tiny length term: straighter paths
            if nc < best.get(n, 1e9):
                best[n] = nc
                prev[n] = cur
                heapq.heappush(pq, (nc, n))
    if b not in best:
        return None, None
    out, cur = [], b
    while cur != a:
        out.append(cur)
        cur = prev[cur]
    out.reverse()
    return best[b], [p for p in out if p != b]


def plan(tiles, cities, W2, start=None, max_dist=6) -> dict:
    T = {(t["x"], t["y"]): t for t in tiles}
    cs = [c for c in cities if (c["x"], c["y"]) in T]
    xy = [(c["x"], c["y"]) for c in cs]
    mass = [T[p].get("mass") for p in xy]
    edges = []
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            if mass[i] != mass[j] or _dist(xy[i], xy[j], W2) > max_dist:
                continue
            cost, path = _path(T, xy[i], xy[j], W2, max_dist + 2)
            if path is not None:
                edges.append((cost, i, j, path))
    edges.sort(key=lambda e: e[0])
    parent = list(range(len(cs)))

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    tree = []
    for cost, i, j, path in edges:
        a, b = find(i), find(j)
        if a != b:
            parent[a] = b
            tree.append((i, j, path))
    # order: breadth-first over the tree from the start cities
    adj = {}
    for i, j, path in tree:
        adj.setdefault(i, []).append((j, path))
        adj.setdefault(j, []).append((i, list(reversed(path))))
    starts = [k for k, c in enumerate(cs) if start and c["index"] in start] or ([0] if cs else [])
    seen, order = set(), []
    for root in starts + list(range(len(cs))):    # start cities first, then the other components
        if root in seen:
            continue
        seen.add(root)
        q = deque([root])
        while q:
            k = q.popleft()
            for j, path in adj.get(k, []):
                if j not in seen:
                    seen.add(j)
                    order.append((cs[k]["name"], cs[j]["name"], path))
                    q.append(j)
    tl, done = [], set()
    for _, _, path in order:
        for p in path:
            if p not in done:
                done.add(p)
                tl.append(p)
    return {"edges": [{"from": a, "to": b, "tiles": len(p)} for a, b, p in order], "tiles": tl}
