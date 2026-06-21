import pytest
from dummyproj.dummyBackend.app import CheckoutService

def test_process_order_index_out_of_range():
    checkout_service = CheckoutService()
    # Create an order data that causes an index out of range for items (e.g., expecting 6 items, providing fewer)
    order_data_items_short = {'items': ['item1', 'item2', 'item3'], 'payment_info': {'method': 'card', 'details': ['detail1']}}
    response_items_short = checkout_service.processOrder(order_data_items_short)
    assert 'index out of range' in response_items_short['error']

    # Create an order data that causes a nil pointer dereference for payment_info (e.g., missing payment_info)
    order_data_no_payment = {'items': ['item1', 'item2', 'item3', 'item4', 'item5', 'item6']}
    response_no_payment = checkout_service.processOrder(order_data_no_payment)
    assert 'nil pointer dereference' in response_no_payment['error']

    # Create an order data that causes a nil pointer dereference for payment details (e.g., missing details)
    order_data_no_payment_details = {'items': ['item1', 'item2', 'item3', 'item4', 'item5', 'item6'], 'payment_info': {'method': 'card', 'details': []}}
    response_no_payment_details = checkout_service.processOrder(order_data_no_payment_details)
    assert 'payment details missing or insufficient' in response_no_payment_details['error']

    # Test a valid order
    valid_order_data = {'items': ['item1', 'item2', 'item3', 'item4', 'item5', 'item6'], 'payment_info': {'method': 'card', 'details': ['detail1']}}
    valid_response = checkout_service.processOrder(valid_order_data)
    assert valid_response['status'] == 'success'
    assert 'Order processed successfully' in valid_response['message']

def test_handle_payment_with_missing_payment_info():
    checkout_service = CheckoutService()
    order_data = {'items': ['item1', 'item2', 'item3', 'item4', 'item5', 'item6']}
    response = checkout_service.processOrder(order_data)
    assert 'nil pointer dereference' in response['error']
