def is_cash_symbol(symbol: str) -> bool:
    return symbol == "CASH" or symbol.startswith("CASH_")
