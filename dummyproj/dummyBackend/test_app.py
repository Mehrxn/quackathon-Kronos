import unittest
from app import App

class TestPaymentHandling(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_handle_payment_empty_details(self):
        order_data = {
            'payment_info': {
                'method': 'card',
                'details': []
            }
        }
        response = self.app.handlePayment(order_data['payment_info'])
        self.assertEqual(response['status'], 'error')
        self.assertIn('payment details missing or insufficient', response['error'])

    def test_handle_payment_valid_details(self):
        order_data = {
            'payment_info': {
                'method': 'card',
                'details': ['some_card_detail']
            }
        }
        response = self.app.handlePayment(order_data['payment_info'])
        self.assertEqual(response['status'], 'success')
        self.assertIn('Payment processed', response['message'])

    def test_handle_payment_paypal(self):
        order_data = {
            'payment_info': {
                'method': 'paypal'
            }
        }
        response = self.app.handlePayment(order_data['payment_info'])
        self.assertEqual(response['status'], 'success')
        self.assertIn('PayPal payment processed', response['message'])

    def test_handle_payment_unsupported_method(self):
        order_data = {
            'payment_info': {
                'method': 'bitcoin'
            }
        }
        response = self.app.handlePayment(order_data['payment_info'])
        self.assertEqual(response['status'], 'error')
        self.assertIn('Invalid or unsupported payment method', response['error'])