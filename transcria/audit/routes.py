import csv
import io
from datetime import datetime, timezone

from flask import Blueprint, Response, render_template, request
from flask_login import login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction, audit_action_label
from transcria.audit.store import AuditStore
from transcria.auth.permissions import Permission, requires
from transcria.auth.store import UserStore

audit_bp = Blueprint("audit", __name__)

PER_PAGE = 50


@audit_bp.route("/admin/audit")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def audit_page():
    page = request.args.get("page", 1, type=int)
    actor = request.args.get("actor", "", type=str).strip()
    action = request.args.get("action", "", type=str).strip()
    ttype = request.args.get("target_type", "", type=str).strip()
    since = request.args.get("since", "", type=str).strip()
    until = request.args.get("until", "", type=str).strip()

    since_dt = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    until_dt = None
    if until:
        try:
            until_dt = datetime.strptime(until, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        except ValueError:
            pass

    actor_id = None
    if actor:
        u = UserStore.get_by_username(actor)
        if u:
            actor_id = u.id
        else:
            actor_id = "___none___"

    rows = AuditStore.query(
        actor_id=actor_id,
        action=action or None,
        target_type=ttype or None,
        since=since_dt,
        until=until_dt,
        limit=PER_PAGE,
        offset=(page - 1) * PER_PAGE,
    )

    total = AuditStore.count(
        actor_id=actor_id,
        action=action or None,
        target_type=ttype or None,
        since=since_dt,
        until=until_dt,
    )

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    return render_template(
        "audit.html",
        rows=rows,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=PER_PAGE,
        filters={
            "actor": actor,
            "action": action,
            "target_type": ttype,
            "since": since,
            "until": until,
        },
        AuditAction=AuditAction,
        action_label=audit_action_label,
    )


@audit_bp.route("/admin/audit/export.csv")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def audit_export_csv():
    since = request.args.get("since", "", type=str).strip()
    until = request.args.get("until", "", type=str).strip()

    since_dt = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    until_dt = None
    if until:
        try:
            until_dt = datetime.strptime(until, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        except ValueError:
            pass

    rows = AuditStore.query(
        since=since_dt, until=until_dt, limit=100_000, offset=0,
    )
    audit_log(
        AuditAction.AUDIT_EXPORT,
        target_type="audit",
        details={
            "format": "csv",
            "since": since or "",
            "until": until or "",
            "row_count": len(rows),
            "limit": 100_000,
            "raw_terms_logged": False,
        },
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "timestamp", "actor_username", "action", "target_type",
        "target_id", "target_label", "ip_address",
    ])
    for row in rows:
        writer.writerow([
            row.timestamp.isoformat(),
            row.actor_username,
            row.action,
            row.target_type,
            row.target_id or "",
            row.target_label,
            row.ip_address,
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_export.csv"},
    )
