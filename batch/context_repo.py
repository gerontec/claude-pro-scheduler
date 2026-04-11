"""ContextRepository — Kontext- und Session-Cache-Abfragen (getrennt von Job-Lifecycle)."""
import time

from .config import get_connection, release_connection, CONTEXT_CACHE_TTL


class ContextRepository:
    """Liest Infrastruktur-Kontext und Session-Cache aus der DB.

    Getrennt von JobRepository damit ContextBuilder kein volles Job-Repository
    als Abhängigkeit braucht.
    """

    def __init__(self):
        self._cache: dict = {'ts': 0.0, 'data': None}

    def get_context_blocks(self) -> tuple[str, str]:
        """Gibt (localhost_text, infra_text) zurück. Gecacht für CONTEXT_CACHE_TTL Sekunden."""
        now = time.time()
        if now - self._cache['ts'] < CONTEXT_CACHE_TTL and self._cache['data']:
            return self._cache['data']

        localhost_text = ''
        infra_text = ''
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT category,label,value FROM ki_localhost_cache "
                    "ORDER BY category,label"
                )
                rows = cur.fetchall()
            if rows:
                lines = ['## Batch-Server localhost (ki_localhost_cache)', '']
                cat = None
                for r in rows:
                    if r['category'] != cat:
                        cat = r['category']
                        lines.append(f"\n### {cat}")
                    lines.append(f"- **{r['label']}**: {r['value']}")
                localhost_text = '\n'.join(lines)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ip_address,hostname,network_range,open_ports,"
                    "services,device_purpose,os_guess FROM ki_infrastructure "
                    "ORDER BY network_range,ip_address"
                )
                rows = cur.fetchall()
            if rows:
                lines = ['## Netzwerk-Infrastruktur (ki_infrastructure)', '']
                for r in rows:
                    p = [f"**{r['ip_address']}**"]
                    if r['hostname']:       p.append(f"({r['hostname']})")
                    if r['network_range']:  p.append(f"[{r['network_range']}]")
                    if r['device_purpose']: p.append(f"→ {r['device_purpose']}")
                    if r['open_ports']:     p.append(f"| Ports: {r['open_ports']}")
                    if r['services']:       p.append(f"| Services: {r['services']}")
                    if r['os_guess']:       p.append(f"| OS: {r['os_guess']}")
                    lines.append('  '.join(p))
                infra_text = '\n'.join(lines)
        finally:
            release_connection(conn)

        self._cache = {'ts': now, 'data': (localhost_text, infra_text)}
        return self._cache['data']

    def get_session_cache(self) -> str | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT JSON_UNQUOTE(JSON_EXTRACT(context_json,'$.summary')) AS s "
                    "FROM claude_context_cache WHERE scope='session-compact' LIMIT 1"
                )
                row = cur.fetchone()
        finally:
            release_connection(conn)
        v = row['s'] if row else None
        return v if v and v != 'NULL' else None
