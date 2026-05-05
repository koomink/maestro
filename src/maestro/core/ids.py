from uuid import uuid4


def new_run_id() -> str:
    return f"run_{uuid4().hex}"
