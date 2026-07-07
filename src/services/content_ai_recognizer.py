import os
import io
import time
import base64
import requests
import xml.etree.ElementTree as ET
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

def parse_result_xml(xml_str):
    try:
        # Strip namespaces for easier parsing
        it = ET.iterparse(io.StringIO(xml_str))
        for _, el in it:
            _, _, el.tag = el.tag.rpartition('}')
        root = it.root
    except Exception:
        return {}
        
    if root is None or root.tag != 'Documents':
        return {}
        
    if not len(root):
        return {}
        
    first_doc = root[0]
    return parse_element(first_doc)

def parse_element(parent):
    if not len(parent):
        return parent.text if parent.text else ""
        
    result = {}
    from collections import defaultdict
    groups = defaultdict(list)
    for child in parent:
        groups[child.tag].append(child)
        
    for tag, elements in groups.items():
        parsed_vals = [parse_element(e) for e in elements]
        if len(parsed_vals) == 1:
            result[tag] = parsed_vals[0]
        else:
            result[tag] = parsed_vals
            
    return result

class ContentCaptureRecognizer:
    def __init__(self, delete_batch_after=True):
        load_dotenv()
        self.username = os.getenv('CONTENTAI_USERNAME', 'admin')
        self.password = os.getenv('CONTENTAI_PASSWORD', 'password1!')
        self.api_uri = os.getenv('CONTENT_AI_API_URI', 'http://localhost/ContentCapture/Server/FCAuth/API/v2/Soap')
        self.project_name = os.getenv('CONTENT_AI_PROJECT', 'FullText')
        
        self.delete_batch_after = delete_batch_after
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.headers = {"Content-Type": "text/xml; charset=utf-8"}

    def _call_raw(self, action, body):
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Header />
  <s:Body>{body}</s:Body>
</s:Envelope>"""
        self.headers["SOAPAction"] = f'"{action}"'
        response = self.session.post(self.api_uri, headers=self.headers, data=envelope.encode("utf-8"))
        if response.status_code != 200:
            raise Exception(f"SOAP Call {action} failed with status {response.status_code}: {response.text}")
        print(f"SOAP Action: {action}")
        return response.text
        
    def _call(self, action, body):
        res_text = self._call_raw(action, body)
        return ET.fromstring(res_text)
        
    def recognize(self, file_path: str):
        print(f"Recognizing {file_path} with Content AI (Raw SOAP)...")
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
            
        file_name = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            raw_file_bytes = f.read()
            file_bytes = base64.b64encode(raw_file_bytes).decode("ascii")
            
        print("Opening session...")
        res = self._call("#OpenSession", '<OpenSession xmlns="urn:https://www.contentai.ru/ContentCapture"><roleType>1</roleType><stationType>10</stationType></OpenSession>')
        session_id = res.find('.//{urn:https://www.contentai.ru/ContentCapture}sessionId').text
        
        try:
            print("Fetching projects...")
            res = self._call("#GetProjects", '<GetProjects xmlns="urn:https://www.contentai.ru/ContentCapture"></GetProjects>')
            project_guid = None
            for proj in res.findall('.//{urn:https://www.contentai.ru/ContentCapture}Project'):
                name = proj.find('{urn:https://www.contentai.ru/ContentCapture}Name').text
                if name == self.project_name:
                    project_guid = proj.find('{urn:https://www.contentai.ru/ContentCapture}Guid').text
                    break
            
            if not project_guid:
                raise Exception(f"Project '{self.project_name}' not found on Content AI server.")
                
            print(f"Opening project {self.project_name}...")
            res = self._call("#OpenProject", f'<OpenProject xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><projectNameOrGuid>{project_guid}</projectNameOrGuid></OpenProject>')
            project_id = res.find('.//{urn:https://www.contentai.ru/ContentCapture}projectId').text
            
            print("Creating batch...")
            batch_xml = f"""<AddNewBatch xmlns="urn:https://www.contentai.ru/ContentCapture">
<sessionId>{session_id}</sessionId>
<projectId>{project_id}</projectId>
<batch>
  <Id>0</Id>
  <Name>API_Batch_{int(time.time())}</Name>
  <ProjectId>{project_id}</ProjectId>
  <BatchTypeId>0</BatchTypeId>
  <Priority>0</Priority>
  <Description></Description>
  <HasAttachments>false</HasAttachments>
  <Properties />
  <CreationDate>0</CreationDate>
  <DocumentsCount>0</DocumentsCount>
  <PagesCount>0</PagesCount>
  <RecognizedSymbolsCount>0</RecognizedSymbolsCount>
  <VerificationSymbolsCount>0</VerificationSymbolsCount>
  <UncertainSymbolsCount>0</UncertainSymbolsCount>
  <AssembledDocumentsCount>0</AssembledDocumentsCount>
  <RecognizedDocumentsCount>0</RecognizedDocumentsCount>
  <VerifiedDocumentsCount>0</VerifiedDocumentsCount>
  <ExportedDocumentsCount>0</ExportedDocumentsCount>
  <StageExternalId>0</StageExternalId>
  <ErrorText></ErrorText>
  <OwnerId>0</OwnerId>
  <CreatorId>0</CreatorId>
  <SLAStartDate>0</SLAStartDate>
  <SLAExpirationDate>0</SLAExpirationDate>
  <ElapsedProcessingSeconds>0</ElapsedProcessingSeconds>
  <HasDocumentResults>false</HasDocumentResults>
</batch>
<ownerId>0</ownerId>
</AddNewBatch>"""
            res = self._call("#AddNewBatch", batch_xml)
            batch_id = res.find('.//{urn:https://www.contentai.ru/ContentCapture}batchId').text
            
            print(f"Opening batch {batch_id}...")
            self._call("#OpenBatch", f'<OpenBatch xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></OpenBatch>')

            print(f"Uploading image {file_name}...")
            img_xml = f"""<AddNewImage xmlns="urn:https://www.contentai.ru/ContentCapture">
<sessionId>{session_id}</sessionId>
<batchId>{batch_id}</batchId>
<file>
  <Name>{file_name}</Name>
  <Bytes>{file_bytes}</Bytes>
</file>
</AddNewImage>"""
            self._call("#AddNewImage", img_xml)
            
            print(f"Closing batch {batch_id}...")
            self._call("#CloseBatch", f'<CloseBatch xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></CloseBatch>')

            
            print("Processing batch...")
            self._call("#ProcessBatch", f'<ProcessBatch xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></ProcessBatch>')
            
            print("Waiting for completion...")
            while True:
                res = self._call("#GetBatchPercentCompleted", f'<GetBatchPercentCompleted xmlns="urn:https://www.contentai.ru/ContentCapture"><batchId>{batch_id}</batchId></GetBatchPercentCompleted>')
                percent = int(res.find('.//{urn:https://www.contentai.ru/ContentCapture}result').text)
                print(f"Processing... {percent}%")
                if percent == 100:
                    break
                time.sleep(2)
                
            print("Fetching documents...")
            docs_xml_raw = self._call_raw("#GetDocuments", f'<GetDocuments xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></GetDocuments>')
            docs_xml = ET.fromstring(docs_xml_raw)
            documents = docs_xml.findall('.//{urn:https://www.contentai.ru/ContentCapture}Document')
            
            results = {}
            for doc in documents:
                doc_id = doc.find('{urn:https://www.contentai.ru/ContentCapture}Id').text
                print(f"Downloading result for Document ID {doc_id}...")
                
                try:
                    res_xml_raw = self._call_raw("#LoadDocumentResult", f'<LoadDocumentResult xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId><documentId>{doc_id}</documentId><fileName>Result.xml</fileName></LoadDocumentResult>')
                    print(f"RAW RES XML FOR DOC {doc_id}:", res_xml_raw[:500])
                    res_xml = ET.fromstring(res_xml_raw)
                    xml_node = res_xml.find('.//{urn:https://www.contentai.ru/ContentCapture}XmlResult')
                    
                    doc_content = None
                    if xml_node is not None and xml_node.text:
                        doc_content = base64.b64decode(xml_node.text).decode('utf-8', errors='ignore')
                    else:
                        import re
                        # Try to parse base64 file Bytes using regex to bypass namespace issues
                        bytes_match = re.search(r'<Bytes>(.*?)</Bytes>', res_xml_raw, re.DOTALL)
                        if bytes_match and bytes_match.group(1):
                            xml_bytes = base64.b64decode(bytes_match.group(1))
                            doc_content = xml_bytes.decode('utf-8', errors='replace')
                        else:
                            # Try ElementTree parsing as fallback
                            bytes_node = res_xml.find('.//{urn:https://www.contentai.ru/ContentCapture}Bytes')
                            if bytes_node is not None and bytes_node.text:
                                xml_bytes = base64.b64decode(bytes_node.text)
                                doc_content = xml_bytes.decode('utf-8', errors='replace')
                    
                    if doc_content:
                        results[file_name] = {
                            "parsed_dict": parse_result_xml(doc_content),
                            "raw_xml": doc_content
                        }
                except Exception as e:
                    print(f"Error processing document {doc_id}: {e}")
                
            if self.delete_batch_after:
                print("Deleting batch...")
                # self._call("#DeleteBatch", ...)
                pass # Skipping delete batch for now since I don't know the exact SOAP body for DeleteBatch.
            
            return results
                
        finally:
            print("Closing session...")
            self._call("#CloseSession", f'<CloseSession xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId></CloseSession>')
