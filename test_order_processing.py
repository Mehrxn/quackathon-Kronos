import pytest

class MockCheckoutService:
    def processOrder(self, order_data):
        items = order_data.get('items', [])
        if not items or len(items) < 5:
            # Simulate index out of range for demonstration
            error_msg = f"processOrder: index out of range [5] with length {len(items)}"
            # Simulate nil pointer dereference in handlePayment
            return {'error': error_msg + ", goroutine 42: handlePayment: nil pointer dereference"}
        return {'status': 'success'}

@pytest.fixture
def checkout_service():
    return MockCheckoutService()

def test_order_processing_with_invalid_items(checkout_service):
    order_data = {'items': [], 'payment_info': {'amount': 100}}
    response = checkout_service.processOrder(order_data)
    assert 'index out of range' in response['error']
    assert 'nil pointer dereference' in response['error']
