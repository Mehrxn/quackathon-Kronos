import unittest
from dummyproj.dummyBackend.app import App

class TestPaymentHandling(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_handle_payment_index_error(self):
        order_data = {
            'payment_info': {
                'method': 'card',
                'details': []
            }
        }
        response = self.app.handlePayment(order_data['payment_info'])
        self.assertEqual(response['status'], 'error')
        self.assertIn('payment details missing or insufficient', response['error'])