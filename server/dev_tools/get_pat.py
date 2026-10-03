import urllib.request, json
req = urllib.request.Request("http://localhost:8000/api/auth/login", data=json.dumps({"employee_id":"DOC001", "password":"password"}).encode(), headers={'Content-Type': 'application/json'})
resp = urllib.request.urlopen(req)
token = json.loads(resp.read())['token']

req2 = urllib.request.Request("http://localhost:8000/api/vigil/patients", headers={'Authorization': 'Bearer ' + token})
resp2 = urllib.request.urlopen(req2)
res_data = json.loads(resp2.read())
print("LENGTH:", len(res_data))
print("FIRST:", res_data[0] if len(res_data) > 0 else "None")
