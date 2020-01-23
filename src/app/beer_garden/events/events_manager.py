# -*- coding: utf-8 -*-
from multiprocessing.connection import Connection

import wrapt
from brewtils.models import Event, Events

import beer_garden

# In entry point processes this will be used to ship events back to the master process
upstream_conn = None


def set_conn(conn: Connection) -> None:
    global upstream_conn
    upstream_conn = conn


def publish_event(event_type):
    # TODO - This is kind of gross
    @wrapt.decorator(enabled=lambda: not getattr(beer_garden, "_running_tests", False))
    def wrapper(wrapped, _, args, kwargs):
        event = Event(name=event_type.name, payload="", error=False)

        try:
            result = wrapped(*args, **kwargs)
        except Exception as ex:
            event.error = True
            event.payload = str(ex)
            raise
        else:
            if event.name in (
                Events.INSTANCE_INITIALIZED.name,
                Events.INSTANCE_STARTED.name,
                Events.INSTANCE_STOPPED.name,
                Events.REQUEST_CREATED.name,
                Events.REQUEST_STARTED.name,
                Events.REQUEST_COMPLETED.name,
                Events.SYSTEM_CREATED.name,
            ):
                event.payload = result
            elif event.name in (
                Events.INSTANCE_UPDATED.name,
                Events.REQUEST_UPDATED.name,
                Events.SYSTEM_UPDATED.name,
            ):
                event.payload = result
                event.metadata = args[1]
            elif event.name in (Events.QUEUE_CLEARED.name, Events.SYSTEM_REMOVED.name):
                event.payload = {"id": args[0]}
            elif event.name in (Events.DB_CREATE.name, Events.DB_UPDATE.name):
                event.payload = result
            elif event.name in (Events.DB_DELETE.name,):
                event.payload = args[0]
        finally:
            upstream_conn.send(event)

        return result

    return wrapper
