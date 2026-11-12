"""MCP server exposing the local RuneScape wiki (2011 snapshot) to Claude.

Wraps the MediaWiki API at http://localhost:8090/api.php, or at $RS_WIKI_API.
Start with: py tools/wiki/mcp_server.py
"""

import asyncio
import os
import re
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server

# RS_WIKI_API points it at another copy, e.g. the hosted one.
WIKI_API = os.environ.get("RS_WIKI_API", "http://localhost:8090/api.php")
TIMEOUT = 10
MAX_CONTENT_LENGTH = 50_000

app = Server("rs-wiki")


async def wiki_request(**params) -> dict:
    params.setdefault("format", "json")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(WIKI_API, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise RuntimeError(
            "Wiki is not running. Start it with:\n"
            "  cd tools/wiki && docker compose up -d\n"
            "Or run: powershell tools/wiki/start-wiki.ps1"
        )
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Wiki API error: HTTP {e.response.status_code}")
    except httpx.ReadTimeout:
        raise RuntimeError("Wiki API timed out. The wiki container may be overloaded.")


def truncate(text: str, limit: int = MAX_CONTENT_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated at {limit} chars, full page is {len(text)} chars]"


def extract_infobox(wikitext: str) -> dict[str, str]:
    """Extract key=value pairs from the first Infobox template in wikitext."""
    match = re.search(r"\{\{[Ii]nfobox[^}]*\n((?:\|[^\n]*\n)*)\}\}", wikitext, re.DOTALL)
    if not match:
        match = re.search(r"\{\{[Ii]nfobox[^|]*(\|(?:[^{}]|\{\{[^{}]*\}\})*)\}\}", wikitext, re.DOTALL)
    if not match:
        return {}

    fields = {}
    for line_match in re.finditer(r"\|\s*(\w[\w\s]*?)\s*=\s*(.*?)(?=\n\||\n\}\}|$)", match.group(0), re.DOTALL):
        key = line_match.group(1).strip()
        val = line_match.group(2).strip()
        val = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", val)
        val = re.sub(r"\{\{[^}]*\}\}", "", val).strip()
        if val:
            fields[key] = val
    return fields


@app.tool()
async def wiki_search(query: str, limit: int = 10) -> str:
    """Search the local RuneScape wiki (May 2011 snapshot, rev 647) for pages matching a query.

    Returns titles and snippets of matching pages. Use this to find wiki articles
    about items, NPCs, quests, skills, locations, or any game content.
    """
    try:
        data = await wiki_request(
            action="query", list="search", srsearch=query, srlimit=str(min(limit, 50))
        )
    except RuntimeError as e:
        return str(e)

    results = data.get("query", {}).get("search", [])
    if not results:
        return f"No results found for '{query}'"

    total = data.get("query", {}).get("searchinfo", {}).get("totalhits", len(results))
    lines = [f"Found {total} results for '{query}':\n"]
    for r in results:
        snippet = re.sub(r"<[^>]+>", "", r.get("snippet", ""))
        lines.append(f"- **{r['title']}** — {snippet}")
    return "\n".join(lines)


@app.tool()
async def wiki_get_page(title: str) -> str:
    """Get the raw wikitext content of a wiki page by exact title.

    Returns full wikitext source including templates, infoboxes, and tables.
    Useful for extracting structured data. Title is case-sensitive
    (e.g., 'Abyssal whip', 'Dragon scimitar', 'Template:Infobox Item').
    """
    try:
        data = await wiki_request(
            action="query", prop="revisions", rvprop="content", titles=title
        )
    except RuntimeError as e:
        return str(e)

    pages = data.get("query", {}).get("pages", {})
    for pid, page_data in pages.items():
        if int(pid) == -1:
            return f"Page '{title}' does not exist in the 2011 wiki snapshot."
        revs = page_data.get("revisions", [])
        if revs:
            content = revs[0].get("*", revs[0].get("content", ""))
            return truncate(content)

    return f"Page '{title}' has no content."


@app.tool()
async def wiki_get_page_html(title: str) -> str:
    """Get rendered HTML of a wiki page. Useful when you need the final displayed
    output rather than raw wikitext. Some templates may not render perfectly
    in this 2011 snapshot.
    """
    try:
        data = await wiki_request(action="parse", page=title, prop="text")
    except RuntimeError as e:
        return str(e)

    if "error" in data:
        return f"Page '{title}' does not exist in the 2011 wiki snapshot."

    html = data.get("parse", {}).get("text", {}).get("*", "")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return truncate(text)


@app.tool()
async def wiki_list_category(category: str, limit: int = 100) -> str:
    """List pages in a wiki category. Pass the category name without 'Category:' prefix.

    Examples: 'Swords', 'Quests', 'Members items', 'Woodcutting'
    """
    try:
        data = await wiki_request(
            action="query",
            list="categorymembers",
            cmtitle=f"Category:{category}",
            cmlimit=str(min(limit, 500)),
        )
    except RuntimeError as e:
        return str(e)

    members = data.get("query", {}).get("categorymembers", [])
    if not members:
        return f"Category '{category}' is empty or does not exist."

    lines = [f"Category:{category} ({len(members)} pages):\n"]
    for m in members:
        lines.append(f"- {m['title']}")
    return "\n".join(lines)


@app.tool()
async def wiki_get_item(name: str) -> str:
    """Look up a RuneScape item by name and return its key stats from the wiki.

    Searches for the item, then extracts data from its Infobox Item template.
    Returns stats like weight, quest requirements, tradability, bonuses, etc.
    """
    try:
        data = await wiki_request(
            action="query", prop="revisions", rvprop="content", titles=name
        )
    except RuntimeError as e:
        return str(e)

    pages = data.get("query", {}).get("pages", {})
    wikitext = ""
    actual_title = name

    for pid, page_data in pages.items():
        if int(pid) == -1:
            search_data = await wiki_request(
                action="query", list="search", srsearch=name, srlimit="1"
            )
            results = search_data.get("query", {}).get("search", [])
            if not results:
                return f"Item '{name}' not found in the 2011 wiki."
            actual_title = results[0]["title"]
            data2 = await wiki_request(
                action="query", prop="revisions", rvprop="content", titles=actual_title
            )
            for _, p2 in data2.get("query", {}).get("pages", {}).items():
                revs = p2.get("revisions", [])
                if revs:
                    wikitext = revs[0].get("*", revs[0].get("content", ""))
            break
        revs = page_data.get("revisions", [])
        if revs:
            wikitext = revs[0].get("*", revs[0].get("content", ""))
            actual_title = page_data.get("title", name)

    if not wikitext:
        return f"Item '{name}' not found or has no content."

    fields = extract_infobox(wikitext)
    if not fields:
        return f"Page '{actual_title}' exists but has no Infobox template.\n\nFirst 2000 chars:\n{wikitext[:2000]}"

    lines = [f"**{actual_title}**\n"]
    for key, val in fields.items():
        lines.append(f"  {key}: {val}")
    return "\n".join(lines)


@app.tool()
async def wiki_get_npc(name: str) -> str:
    """Look up a RuneScape NPC or monster by name and return its key info.

    Searches for the NPC, then extracts data from its Infobox NPC/Monster template.
    Returns combat level, examine text, locations, drops, etc.
    """
    try:
        data = await wiki_request(
            action="query", prop="revisions", rvprop="content", titles=name
        )
    except RuntimeError as e:
        return str(e)

    pages = data.get("query", {}).get("pages", {})
    wikitext = ""
    actual_title = name

    for pid, page_data in pages.items():
        if int(pid) == -1:
            search_data = await wiki_request(
                action="query", list="search", srsearch=name, srlimit="1"
            )
            results = search_data.get("query", {}).get("search", [])
            if not results:
                return f"NPC '{name}' not found in the 2011 wiki."
            actual_title = results[0]["title"]
            data2 = await wiki_request(
                action="query", prop="revisions", rvprop="content", titles=actual_title
            )
            for _, p2 in data2.get("query", {}).get("pages", {}).items():
                revs = p2.get("revisions", [])
                if revs:
                    wikitext = revs[0].get("*", revs[0].get("content", ""))
            break
        revs = page_data.get("revisions", [])
        if revs:
            wikitext = revs[0].get("*", revs[0].get("content", ""))
            actual_title = page_data.get("title", name)

    if not wikitext:
        return f"NPC '{name}' not found or has no content."

    fields = extract_infobox(wikitext)
    if not fields:
        return f"Page '{actual_title}' exists but has no Infobox template.\n\nFirst 2000 chars:\n{wikitext[:2000]}"

    lines = [f"**{actual_title}**\n"]
    for key, val in fields.items():
        lines.append(f"  {key}: {val}")
    return "\n".join(lines)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
