"""Fee formula against Polymarket's published table and an on-chain fill."""

import duckdb
import pytest

from polydata.fees import SQL_MACRO, fee_fraction_of_cost, taker_fee


@pytest.mark.parametrize("price, fee_005, fee_001", [
    # docs.polymarket.com/trading/fees, 100 shares (table shows cents)
    (0.01, 0.05, 0.01), (0.10, 0.45, 0.09), (0.30, 1.05, 0.21), (0.50, 1.25, 0.25), (0.70, 1.05, 0.21), (0.95, 0.24, 0.05),
])
def test_published_table(price, fee_005, fee_001):
    assert round(taker_fee(100, price, 0.05), 2) == fee_005
    assert round(taker_fee(100, price, 0.01), 2) == fee_001


def test_onchain_fill():
    # tx 0x17cbc1ed…992f: taker bought 15.625 Down at 0.32 on btc-updown-5m; on-chain fee 170000 (6 decimals)
    assert taker_fee(15.625, 0.32, 0.05) == 0.17


def test_rounding_and_symmetry():
    assert taker_fee(1, 0.001, 0.01) == 0.00001  # 0.00000999 -> 0.00001
    assert taker_fee(1, 0.0001, 0.01) == 0.0     # below the smallest fee
    assert taker_fee(10, 0.3, 0.05) == taker_fee(10, 0.7, 0.05)
    assert fee_fraction_of_cost(0.5, 0.05) == pytest.approx(0.025)


def test_sql_macro_matches_python():
    con = duckdb.connect()
    con.execute(SQL_MACRO)
    for shares, p, rate in [(15.625, 0.32, 0.05), (100, 0.5, 0.01), (3.3, 0.987, 0.05)]:
        assert con.sql(f"SELECT taker_fee({shares}, {p}, {rate})").fetchall()[0][0] == pytest.approx(taker_fee(shares, p, rate), abs=1e-12)
