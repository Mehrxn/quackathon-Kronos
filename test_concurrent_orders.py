import threading
import requests
import time

def make_concurrent_requests():
    base_url = 'http://localhost:8080'
    num_requests = 100
    threads = []

    def post_order():
        try:
            response = requests.post(f'{base_url}/order', json={'items': [1, 2, 3, 4, 5, 6]})
            response.raise_for_status()
            print(f"Order POST response: {response.status_code} {response.text}")
            order_id = int(response.text.split(' ')[1]) # Extract order ID
            # Now try to make a payment for this order
            payment_response = requests.post(f'{base_url}/payment?order_id={order_id}')
            payment_response.raise_for_status()
            print(f"Payment POST response for order {order_id}: {payment_response.status_code} {payment_response.text}")
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")

    print(f"Starting {num_requests} concurrent requests...")
    for _ in range(num_requests):
        thread = threading.Thread(target=post_order)
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()
    print("All concurrent requests finished.")

if __name__ == '__main__':
    # Give the Go server a moment to start up if run in sequence
    print("Waiting for server to start...")
    time.sleep(2)
    make_concurrent_requests()
