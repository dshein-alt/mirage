import threading
import time

import pytest

from mirage.errors import WorkbenchError


def test_maintenance_blocks_new_work_and_drains_admitted_writes(app):
    database = app.database
    with database.write_admission():
        database.enter_maintenance("update-test")
        assert database.maintenance_status()["admitted_writes"] == 1
        with pytest.raises(WorkbenchError, match="maintenance"):
            database.create_job("generation", {"prompt": "closed"})
        with pytest.raises(WorkbenchError, match="maintenance"):
            with database.write_admission():
                pass
        assert database.next_job("generation") is None
    assert database.active_write_count() == 0
    database.leave_maintenance("update-test")
    assert database.maintenance_status()["active"] is False


def test_busy_installation_cannot_enter_maintenance(app, registry):
    from conftest import install_sparse

    install_sparse(registry, "zimage")
    app.submit_generation({"prompt": "Busy", "model": "zimage"})
    with pytest.raises(WorkbenchError, match="queued or running"):
        app.database.enter_maintenance("update-test")
    assert app.database.maintenance_status()["active"] is False
