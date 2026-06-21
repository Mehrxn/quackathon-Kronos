import pytest
from demo import PaymentService

def test_process_order_index_out_of_range():
    service = PaymentService()
    order_data = {'items': [1, 2, 3]}
    result = service.processOrder(order_data)
    assert 'error' in result
    assert 'index out of range' in result['error']
    assert 'length 3' in result['error']

def test_process_order_sufficient_items():
    service = PaymentService()
    order_data = {'items': [1, 2, 3, 4, 5, 6]}
    result = service.processOrder(order_data)
    assert 'status' in result
    assert result['status'] == 'success'
    assert 'processed_items' in result

def test_process_order_empty_items():
    service = PaymentService()
    order_data = {'items': []}
    result = service.processOrder(order_data)
    assert 'error' in result
    assert 'index out of range' in result['error']
    assert 'length 0' in result['error']
