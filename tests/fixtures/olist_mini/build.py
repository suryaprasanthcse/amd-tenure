"""The 7-table olist_mini fixture from master Appendix T (test_io.py write_fixture), moved here verbatim."""
import pandas as pd

from tenure.olist import FILES


def _order(oid, cid, status, purchased, delivered, estimated):
    return dict(order_id=oid, customer_id=cid, order_status=status, order_purchase_timestamp=purchased,
                order_approved_at="", order_delivered_carrier_date="", order_delivered_customer_date=delivered,
                order_estimated_delivery_date=estimated)


def _item(oid, n, pid, price, freight):
    return dict(order_id=oid, order_item_id=n, product_id=pid, seller_id="s", shipping_limit_date="",
                price=price, freight_value=freight)


def _review(rid, oid, score, title, msg, created):
    return dict(review_id=rid, order_id=oid, review_score=score, review_comment_title=title,
                review_comment_message=msg, review_creation_date=created, review_answer_timestamp="")


def write_fixture(d):
    pd.DataFrame([
        _order("o1", "c1", "delivered", "2018-02-01 10:00:00", "2018-02-20 12:00:00", "2018-02-14 00:00:00"),
        _order("o2", "c2", "shipped", "2018-03-01 10:00:00", "", "2018-03-10 00:00:00"),
        _order("o3", "c3", "delivered", "2018-04-01 10:00:00", "2018-04-05 09:00:00", "2018-04-15 00:00:00"),
    ]).to_csv(d / FILES["orders"], index=False)
    pd.DataFrame([_item("o1", "1", "p1", "100.00", "20.00"), _item("o1", "2", "p1", "100.00", "20.00"),
                  _item("o2", "1", "p2", "59.90", "0.00"), _item("o3", "1", "p2", "30.00", "10.00")]
                 ).to_csv(d / FILES["items"], index=False)
    pd.DataFrame([dict(order_id="o1", payment_sequential="1", payment_type="credit_card", payment_installments="1",
                       payment_value="240.00"),
                  dict(order_id="o2", payment_sequential="1", payment_type="boleto", payment_installments="1",
                       payment_value="59.90")]).to_csv(d / FILES["payments"], index=False)
    pd.DataFrame([
        _review("r1", "o1", "1", "Atraso", "Chegou muito atrasado, vou no Procon", "2018-02-21 00:00:00"),
        _review("r1", "o1", "1", "Atraso", "duplicate", "2018-02-21 00:00:00"),
        _review("r2", "o2", "2", "", "Ainda não recebi", "2018-03-20 00:00:00"),
        _review("r3", "o3", "5", "", "", "2018-04-06 00:00:00"),
    ]).to_csv(d / FILES["reviews"], index=False)
    pd.DataFrame([dict(customer_id=c, customer_unique_id="u" + c, customer_zip_code_prefix="", customer_city="",
                       customer_state="SP") for c in ("c1", "c2", "c3")]).to_csv(d / FILES["customers"], index=False)
    pd.DataFrame([dict(product_id="p1", product_category_name="cama_mesa_banho"),
                  dict(product_id="p2", product_category_name="")]).to_csv(d / FILES["products"], index=False)
    pd.DataFrame([dict(product_category_name="cama_mesa_banho", product_category_name_english="bed_bath_table")]
                 ).to_csv(d / FILES["translation"], index=False)
