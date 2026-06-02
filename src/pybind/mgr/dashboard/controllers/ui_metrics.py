# -*- coding: utf-8 -*-
"""
Dashboard UI Metrics controller.

Tracks anonymous UI usage:
  - Page visit counts per page (incremented by server-side API hooks)
  - Session login count and last-login timestamp (posted by Angular on login)
  - Storage protocol detection via pool application_metadata

KV keys (all under shared mgr store, readable by telemetry + prometheus modules):
  ui_metrics/page_visits/<page>           int
  ui_metrics/sessions/login_count         int
  ui_metrics/sessions/last_login_epoch    int (unix epoch)
  ui_metrics/protocols_enabled            json str
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .. import mgr
from ..exceptions import DashboardException
from ..security import Scope
from . import APIDoc, APIRouter, Endpoint, EndpointDoc, RESTController

logger = logging.getLogger('controllers.ui_metrics')

# Canonical page names — must match keys used in Angular router paths
KNOWN_PAGES: List[str] = [
    'overview',
    'pools',
    'hosts',
    'block-images',
    'filesystem',
    'object-users',
    'object-buckets',
    'alerts',
]

KV_PAGE_PREFIX = 'ui_metrics/page_visits/'
KV_LOGIN_COUNT = 'ui_metrics/sessions/login_count'
KV_LAST_LOGIN = 'ui_metrics/sessions/last_login_epoch'
KV_PROTOCOLS = 'ui_metrics/protocols_enabled'
KV_USER_PERSONAS = 'ui_metrics/user_personas'


# ---------------------------------------------------------------------------
# Public helper — called from existing controllers to increment page counters
# ---------------------------------------------------------------------------

def increment_page_visit(page: str) -> None:
    """
    Increment visit counter for a known page.
    Called from existing page controllers (pools, hosts, etc.) on their
    list() / get() methods so we never need to touch Angular components.
    """
    if page not in KNOWN_PAGES:
        return
    key = KV_PAGE_PREFIX + page
    raw = mgr.get_store(key)
    mgr.set_store(key, str((int(raw) if raw else 0) + 1))


def record_login() -> None:
    """Called from auth controller on successful login."""
    raw = mgr.get_store(KV_LOGIN_COUNT)
    mgr.set_store(KV_LOGIN_COUNT, str((int(raw) if raw else 0) + 1))
    mgr.set_store(KV_LAST_LOGIN, str(int(time.time())))


def collect_metrics() -> Dict[str, Any]:
    """Returns all stored metrics as a plain dict. Used by REST, telemetry, prometheus."""
    page_visits: Dict[str, int] = {
        page: int(mgr.get_store(KV_PAGE_PREFIX + page) or 0)
        for page in KNOWN_PAGES
    }

    prot_raw = mgr.get_store(KV_PROTOCOLS)
    if prot_raw:
        protocols = json.loads(prot_raw)
    else:
        protocols = _detect_and_cache_protocols()

    return {
        'page_visits': page_visits,
        'sessions': {
            'login_count': int(mgr.get_store(KV_LOGIN_COUNT) or 0),
            'last_login_epoch': int(mgr.get_store(KV_LAST_LOGIN) or 0),
        },
        'protocols_enabled': protocols,
        'user_personas': (
            json.loads(mgr.get_store(KV_USER_PERSONAS))
            if mgr.get_store(KV_USER_PERSONAS)
            else _collect_user_personas()
        ),
    }


def _detect_and_cache_protocols() -> Dict[str, Any]:
    """
    Inspect osd_map pool application_metadata to determine which protocols
    are in use:
        'rbd'    → Block  (RBD)
        'cephfs' → File   (CephFS)
        'rgw'    → Object (RGW / S3)
    Result is cached in KV store so telemetry and prometheus can read it
    without re-inspecting osd_map every scrape.
    """
    try:
        osd_map = mgr.get('osd_map') or {}
        pools = osd_map.get('pools', [])
    except Exception as e:  # pylint: disable=broad-except
        logger.error('ui_metrics: failed to read osd_map: %s', e)
        return {'block': False, 'file': False, 'object': False}

    result: Dict[str, Any] = {
        'block':  any('rbd'    in (p.get('application_metadata') or {}) for p in pools),
        'file':   any('cephfs' in (p.get('application_metadata') or {}) for p in pools),
        'object': any('rgw'    in (p.get('application_metadata') or {}) for p in pools),
        'pools_checked': len(pools),
    }
    mgr.set_store(KV_PROTOCOLS, json.dumps(result))
    return result


def _collect_user_personas() -> Dict[str, int]:
    """
    Aggregate dashboard user personas from persisted RBAC database.
    Privacy-safe: only aggregated counts, no usernames.
    """
    personas = {
        'admin': 0,
        'read_only': 0,
        'block_operator': 0,
        'file_system_operator': 0,
        'object_storage_operator': 0,
        'monitoring': 0,
    }

    try:
        db = mgr.get_store('mgr/dashboard/accessdb_v2')

        logger.warning('ui_metrics: raw accessdb=%s', db)

        if not db:
            logger.warning('ui_metrics: accessdb_v2 not found')
            return personas

        db = json.loads(db)

        logger.warning('ui_metrics: parsed db keys=%s', db.keys())

        users = db.get('users', {})

        logger.warning('ui_metrics: users=%s', users)

        for _, user in users.items():

            logger.warning('ui_metrics: processing user=%s', user)

            roles = [
                r.lower()
                for r in user.get('roles', [])
            ]

            logger.warning('ui_metrics: roles=%s', roles)

            if 'administrator' in roles:
                personas['admin'] += 1

            if 'read-only' in roles:
                personas['read_only'] += 1

            if 'rbd-manager' in roles:
                personas['block_operator'] += 1

            if 'cephfs-manager' in roles:
                personas['file_system_operator'] += 1

            if 'rgw-manager' in roles:
                personas['object_storage_operator'] += 1

            if 'cluster-manager' in roles:
                personas['monitoring'] += 1

    except Exception as e:  # pylint: disable=broad-except
        logger.error(
            'ui_metrics: failed collecting personas: %s',
            e
        )

    logger.warning('ui_metrics: final personas=%s', personas)

    mgr.set_store(KV_USER_PERSONAS, json.dumps(personas))

    return personas


# ---------------------------------------------------------------------------
# REST Controller
# ---------------------------------------------------------------------------

# @APIRouter('/ui_metrics', Scope.MONITOR)
@APIRouter('/ui_metrics')
@APIDoc('Dashboard UI usage metrics (anonymous, aggregated)', 'UIMetrics')
class UIMetrics(RESTController):
    """
    GET  /api/ui_metrics            — returns all aggregated metrics
    POST /api/ui_metrics            — records a session_login event
    GET  /api/ui_metrics/protocols  — returns protocol detection result
    """

    def list(self) -> Dict[str, Any]:
        """Return all aggregated UI metrics."""
        return collect_metrics()

    def create(self, event_type: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/ui_metrics
        Body: { "event_type": "session_login" }
        Only session_login is accepted here; page visits are
        server-side increments in existing controllers.
        """
        if event_type != 'session_login':
            raise DashboardException(
                msg='Only event_type="session_login" is accepted',
                http_status_code=400,
                component='ui_metrics')
        record_login()
        return {'status': 'ok', 'event_type': 'session_login'}

    @Endpoint()
    @EndpointDoc('Detect enabled storage protocols from pool application tags')
    def protocols(self) -> Dict[str, Any]:
        """GET /api/ui_metrics/protocols — refreshes and returns protocol detection."""
        return _detect_and_cache_protocols()

