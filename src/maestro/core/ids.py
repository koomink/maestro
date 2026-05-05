from uuid import uuid4


def new_run_id() -> str:
    return f"run_{uuid4().hex}"


def new_order_id() -> str:
    return f"ord_{uuid4().hex}"


def new_approval_id() -> str:
    return f"appr_{uuid4().hex}"
