import requests
    
url = "https://www.onemap.gov.sg/api/common/convert/4326to3857?latitude=1.29441667 &longitude=103.77933333"
    
headers = {"Authorization": "**********************"}
    
response = requests.request("GET", url, headers=headers)
    
print(response.text)
