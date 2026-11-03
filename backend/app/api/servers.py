"""Remote servers: add machines over SSH, check them, run commands and manage their containers, and pick them as
deploy targets. Everything here can run code on those machines, so it needs the same sign-in as deploying."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agents import runner
from ..db import get_db
from ..services import servers
from .apps import require_admin, user

router = APIRouter(prefix="/api/servers", tags=["remote servers"], dependencies=[Depends(require_admin)])


def _err(e: servers.ServerError):
    return HTTPException(e.status, str(e))


def _server(db: Session, server_id: int):
    try:
        return servers.get(db, server_id)
    except servers.ServerError as e:
        raise _err(e) from e


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    host: str = Field(min_length=1, max_length=255)
    username: str = "root"
    ssh_port: int = 22
    private_key: str | None = None  # empty: DevNexa generates a key pair
    address: str = ""
    port_base: int = 20000
    actor: str = "operator"


class ServerPatch(BaseModel):
    host: str | None = None
    username: str | None = None
    ssh_port: int | None = None
    private_key: str | None = None
    regenerate_key: bool = False
    address: str | None = None
    port_base: int | None = None
    actor: str = "operator"


class CommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    timeout: int | None = Field(default=None, ge=5, le=600)
    actor: str = "operator"


class Actor(BaseModel):
    actor: str = "operator"


@router.get("")
def list_servers(db: Session = Depends(get_db)):
    return [servers.to_dict(db, s) for s in servers.list_all(db)]


@router.post("", status_code=201)
def add_server(body: ServerIn, request: Request, db: Session = Depends(get_db)):
    try:
        s = servers.create(db, body.name, body.host, body.username.strip(), body.ssh_port, body.private_key,
                           body.address, body.port_base, user(request) or body.actor)
    except servers.ServerError as e:
        raise _err(e) from e
    db.commit()
    return servers.to_dict(db, s)


@router.get("/{server_id}")
def get_server(server_id: int, db: Session = Depends(get_db)):
    return servers.to_dict(db, _server(db, server_id))


@router.patch("/{server_id}")
def edit_server(server_id: int, body: ServerPatch, request: Request, db: Session = Depends(get_db)):
    s = _server(db, server_id)
    fields = body.model_dump(exclude={"actor"})
    try:
        servers.update(db, s, user(request) or body.actor, **fields)
    except servers.ServerError as e:
        raise _err(e) from e
    db.commit()
    return servers.to_dict(db, s)


@router.delete("/{server_id}")
def remove_server(server_id: int, request: Request, actor: str = "operator", db: Session = Depends(get_db)):
    s = _server(db, server_id)
    try:
        servers.delete(db, s, user(request) or actor)
    except servers.ServerError as e:
        raise _err(e) from e
    db.commit()
    return {"deleted": server_id}


@router.post("/{server_id}/check")
def check_server(server_id: int, db: Session = Depends(get_db)):
    """Log in over SSH and read the machine: OS, CPU, memory, disk, Docker and its containers."""
    s = _server(db, server_id)
    result = servers.check(db, s)
    db.commit()
    return {**result, "server": servers.to_dict(db, s)}


@router.post("/{server_id}/exec")
def exec_command(server_id: int, body: CommandIn, request: Request, db: Session = Depends(get_db)):
    """Run one shell command on the server as its SSH user (audited)."""
    s = _server(db, server_id)
    try:
        out = servers.run_command(db, s, body.command, user(request) or body.actor, body.timeout)
    except servers.ServerError as e:
        raise _err(e) from e
    db.commit()
    return out


@router.post("/{server_id}/containers/{container}/{action}")
def container_action(server_id: int, container: str, action: str, body: Actor, request: Request,
                     db: Session = Depends(get_db)):
    s = _server(db, server_id)
    try:
        out = servers.container_action(db, s, container, action, user(request) or body.actor)
    except servers.ServerError as e:
        raise _err(e) from e
    db.commit()
    return out


@router.post("/{server_id}/install-docker", status_code=202)
def install_docker(server_id: int, body: Actor, request: Request, db: Session = Depends(get_db)):
    s = _server(db, server_id)
    if (s.job or {}).get("status") == "running":
        raise HTTPException(409, "A job is already running on this server.")
    runner.submit(servers.install_docker, server_id, user(request) or body.actor)
    return {"status": "started"}
