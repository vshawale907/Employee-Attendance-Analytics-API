import requests, time
base = 'http://localhost:8000'
time.sleep(2)

r = requests.get(f'{base}/health')
print(f'BUG 7 /health: {r.status_code} {r.json()}')

r = requests.post(f'{base}/attendance/punch-in', json={'emp_code':'DOESNOTEXIST','status':'PRESENT'})
print(f'BUG 12 unknown emp: HTTP {r.status_code}  (should be 404)')
print(f'  body snippet: {r.text[:150]}')

r = requests.get(f'{base}/employees', params={'page': 1, 'page_size': 20})
d = r.json()
print(f'BUG 11 page=1: total={d["total"]} items={len(d["items"])}  (starter skips 20, should return up to 6)')

r = requests.post(f'{base}/employees', json={'emp_code':'INVALID','name':'x','email':'x@x.com','department':'Eng','joined_on':'2026-01-01'})
print(f'BUG 5 bad emp_code: HTTP {r.status_code}  (should be 422)')

r = requests.post(f'{base}/attendance/punch-in', json={'emp_code':'EMP0001','punched_at':1234567,'status':'PRESENT'})
print(f'BUG 6 seconds timestamp: HTTP {r.status_code}  (should be 422)')

r = requests.post(f'{base}/attendance/punch-in', json={'emp_code':'EMP0001','status':'ABSENT'})
print(f'BUG 6 ABSENT on punch-in: HTTP {r.status_code}  (should be 422)')
