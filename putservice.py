import xml.etree.ElementTree as ET
import defusedxml.ElementTree as ETdefused
from flask import Flask, request, abort, jsonify, Response
from flask import current_app as app
from flask_cors import CORS, cross_origin
from flask_elasticsearch import FlaskElasticsearch
import urllib.request, urllib.parse, urllib.error
import elastic, communication
import ipaddress
import datetime
import base64
from dateutil.relativedelta import relativedelta
import botocore.session, botocore.client
from botocore.exceptions import ClientError
import hashlib


################
# PUT Variables
################


peerIdents = ["WebHoneypot", "Webpage",
              "dionaea", "Network(Dionaea)",
              "honeytrap", "Network(honeytrap)",
              "kippo", "SSH/console(cowrie)",
              "cowrie", "SSH/console(cowrie)",
              "glastopf", "Webpage",
              ".gt3",  "Webpage",
              ".dio", "Network(Dionaea)",
              ".kip", "SSH/console(cowrie)",
              ".ht", "Network(honeytrap)",
              "vnclowpot", "VNC(vnclowpot)",
              "rdpy", "RDP(rdpy)",
              "mailoney", "E-Mail(mailoney)",
              "heralding", "Passwords(heralding)",
              "ciscoasa", "Network(cisco-asa)",
              "elasticpot", "Webpage",
              "suricata", "Network(suricata)"
              "", ""]

################
# PUT functions
################



def checkPostData(postrequest):
    """check if postdata is XML"""
    postdata = postrequest.decode('utf-8')
    try:
        return ETdefused.fromstring(postdata)
    except ETdefused.ParseError:
        app.logger.error('Invalid XML in post request')
        return False

def getPeerType(id):
    """
        get the peerType from peerIdent
    """
    for i in range (0,len(peerIdents) - 2, 2):
         honeypot = peerIdents[i]
         peerType = peerIdents[i+1]

         if (honeypot in id):
             return peerType

    return "Unclassified"

def fixUrl(destinationPort, transport, url, peerType):
    """
        fixes the URL (original request string)
    """
    transportProtocol = ""
    if transport.lower() in "udp" or transport.lower() in "tcp":
        transportProtocol="/"+transport

    if ("honeytrap" in peerType):
        return "Attack on port " + str(destinationPort) + transportProtocol

    # prepared dionaea to output additional information in ticker
    if ("Dionaea" in peerType):
        return "Attack on port " + str(destinationPort)+ transportProtocol

    return url

def handleAlerts(tree, tenant, es, cache, s3client):
    """
        parse the xml, handle the Alerts and send to es
    """
    counter = 0
    for node in tree.findall('.//Alert'):
        # default values
        parsingError = ""
        skip = False
        additionalData, packetdata, peerType, vulnid, source, sourcePort, destination, destinationPort, createTime, url, analyzerID, username, password, loginStatus, version, starttime, endtime, externalIP, internalIP, hostname, sourceTransport, rawhttp = {}, "","Unclassified", "", "","", "", "", "-", "", "", "", "", "", "", "", "", "1.1.1.1", "1.1.1.1", "undefined", "", "-"
        for child in node:
            childName = child.tag

            if (childName == "Analyzer"):
                if child.attrib.get('id') is not "":
                    analyzerID = child.attrib.get('id')
                else:
                    parsingError += "analyzerID = '' "
                if analyzerID is not "":
                    peerType = getPeerType(analyzerID)

            if (childName == "Source"):
                if child.text is not None and testIPAddress(child.text):
                    source = child.text.replace('"', '')
                else:
                    parsingError += "| source = NONE "
                sourcePort = child.attrib.get('port')
                sourceTransport = child.attrib.get('protocol')

            if (childName == "CreateTime"):
                if child.text is not None:
                    createTime = child.text

                    # time conversion to utc from honeypot's localtime using timezone transmitted.
                    # must ignore this for honeytrap, as it already sends utc no matter what the system time is.
                    if not "honeytrap" in peerType.lower():
                        if child.attrib.get('tz') is not "":
                            timezone=child.attrib.get('tz')
                            if timezone != "+0000":
                                createTimeOld=createTime
                                createTime=calculateUTCTime(createTime, timezone)
                                app.logger.debug("Calculating new timezone for " + createTimeOld + " timezone: " + timezone + " => " + createTime)
                        else:
                            parsingError += "| timezone = NONE "


                else:
                    parsingError += "| CreateTime = NONE "

            if (childName == "Target"):
                if child.text is not None and testIPAddress(child.text) :
                    destination = child.text.replace('"', '')
                else:
                    parsingError += "| destination = NONE "
                destinationPort = child.attrib.get('port')

            if (childName == "Request"):
                type = child.attrib.get('type')

                if (type == "url"):
                    if child.text is not None:
                        url = urllib.parse.unquote(child.text)
                    else:
                        parsingError += "| url = NONE "

                if (type == "raw" or type == "binary"):
                    if child.text is not None:
                        try:
                            m=hashlib.md5()
                            m.update(base64.b64decode(child.text.encode()))
                            md5sum=m.hexdigest()
                            rawhttpcand = base64.b64decode(child.text).decode("UTF-8")
                            httpMethods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'CONNECT', 'HEAD', 'OPTIONS', 'TRACE']

                            if any(x in rawhttpcand.split(" ")[0] for x in httpMethods):
                                app.logger.debug("PeerType: %s - Storing HTTP RAW data from RAW / BINARY payload (plain) in httpraw: %s" % (peerType, str(base64.b64decode(child.text))[0:50]+"..."))
                                rawhttp = rawhttpcand
                                packetdata = child.text
                                additionalData["payload_md5"] = md5sum

                            else:
                                app.logger.debug("PeerType: %s - Storing ASCII data from RAW / BINARY payload (base64) in packetdata: %s" % (peerType, str(base64.b64decode(child.text))[0:50]+"..."))
                                packetdata = child.text
                                additionalData["payload_md5"] = md5sum

                        except:
                            app.logger.debug("PeerType: %s - Storing BINARY from  RAW / BINARY payload (base64) in packetdata: %s" % (peerType, str(base64.b64decode(child.text))[0:50]+"..."))
                            packetdata = child.text
                            additionalData["payload_md5"] = md5sum

                    else:
                        parsingError +="| httpraw = NONE "

                # if peertype could not be identified by the identifier of the honeypot, try to use the
                # description field
                if (type == "description" and peerType == ""):
                    peerType = getPeerType(child.text)

            if (childName == "AdditionalData"):

                meaning = child.attrib.get('meaning')

                if (meaning == "username"):
                    username = child.text

                if (meaning == "password"):
                    password = child.text

                if (meaning == "login"):
                    loginStatus = child.text

                if (meaning == "version"):
                    version = child.text

                # starttime (cowrie) must be present
                if (meaning == "starttime"):
                    if child.text is not None:
                        starttime = urllib.parse.unquote(child.text)
                    else:
                        parsingError += "| starttime = NONE "

                # endtime (cowrie) is not necessarily set
                if (meaning == "endtime"):
                    if child.text is not None:
                        endtime = urllib.parse.unquote(child.text)

                # cveid (suricata) must be set, otherwise discard.
                if (meaning == "cve_id"):
                    if child.text is not None:
                        vulnid = urllib.parse.unquote(child.text)
                    else:
                        parsingError += "| cve_id = NONE "

                # input (cowrie) not necessarily set, might be an empty session
                if (meaning == "input"):
                    if child.text is not None:
                        url = urllib.parse.unquote(child.text).replace('\n', '; ')[2:]

                if (meaning == "externalIP"):
                    if child.text is not None:
                        externalIP = child.text

                if (meaning == "internalIP"):
                    if child.text is not None:
                        internalIP = child.text

                if (meaning == "hostname"):
                    if child.text is not None:
                        hostname = child.text

                # Todo: add additional data from ewsposter fields as json structure.

                # for heralding
                if (meaning == "protocol"):
                    if child.text is not None:
                        additionalData["protocol"] = child.text

                # for cisco-asa
                if (meaning == "payload"):
                    if child.text is not None:
                        additionalData["payload"] = urllib.parse.unquote(child.text)

                # for dionaea binaries/honeytrap payloads/glastopf rfis
                if (meaning == "payload_md5"):
                    if child.text is not None:
                        additionalData["payload_md5"] = child.text


        if parsingError is not "":
            app.logger.debug("Skipping incomplete ews xml alert element : " + parsingError)
            skip = True

        if not skip:
            url = fixUrl(destinationPort, sourceTransport, url, peerType)

            #
            # persist CVE
            #
            if (len(str(vulnid)) > 2):
                elastic.putVuln(vulnid, "ewscve", source, destination, createTime,
                                              tenant, url,
                                              analyzerID, peerType, username, password, loginStatus, version, starttime,
                                              endtime, sourcePort, destinationPort, externalIP, internalIP, hostname,
                                              sourceTransport, additionalData, app.config['DEVMODE'], es, cache, packetdata, rawhttp)
                url = "(" + vulnid + ") " + url

            #
            # store attack itself
            #
            correction = elastic.putAlarm(vulnid, app.config['ELASTICINDEX'], source, destination, createTime, tenant, url,
                                          analyzerID, peerType, username, password, loginStatus, version, starttime,
                                          endtime, sourcePort, destinationPort, externalIP, internalIP, hostname, sourceTransport, additionalData, app.config['DEVMODE'], es, cache, packetdata, rawhttp)
            counter = counter + 1 - correction

            if s3client and (packetdata is not ""):
                try:
                    # check if file exists in bucket
                    searchFile = s3client.list_objects_v2(Bucket=app.config['S3BUCKET'], Prefix=additionalData["payload_md5"])
                    if (len(searchFile.get('Contents', []))) == 1 and str(
                            searchFile.get('Contents', [])[0]['Key']) == additionalData["payload_md5"]:
                        app.logger.error(
                            'Not storing file ({0}) to s3 bucket "{1}" on {2} as it already exists in the bucket.'.format(
                                additionalData["payload_md5"], app.config['S3BUCKET'], app.config['S3ENDPOINT']))
                    else:
                        # upload file to s3
                        bodydata=base64.decodebytes(packetdata.encode('utf-8'))
                        s3client.put_object(Bucket=app.config['S3BUCKET'], Body=bodydata, Key=additionalData["payload_md5"])
                        app.logger.error('Storing file ({0}) using s3 bucket "{1}" on {2}'.format(additionalData["payload_md5"], app.config['S3BUCKET'], app.config['S3ENDPOINT']))

                except ClientError as e:
                    app.logger.error("Received error: %s", e.response['Error']['Message'])

            #
            # slack wanted
            #
            if (app.config['USESLACK']):
                if len(str(app.config['SLACKTOKEN'])) > 10:
                    if len(str(vulnid)) > 4:
                        if (elastic.cveExisting(vulnid, "ewscve", es, app.config['DEVMODE'])):
                            communication.sendSlack("cve", app.config['SLACKTOKEN'], "CVE (" + vulnid + ") found.", app.config['DEVMODE'])

    app.logger.debug("Info: Added " + str(counter) + " entries")
    return True

def testIPAddress(ip):
    ''' test if it is an ipv4 address'''
    try:
        ipaddress.IPv4Address(ip)
        return True
    except:
        return False

def calculateUTCTime(timestamp, timezone):
    ''' function to calculate localtime from utc time and timezone.'''
    operand=timezone[0]
    timedelta=timezone[1:3]
    timedeltaMin=timezone[3:5]
    if operand == "+":
        createTime= datetime.datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S") + relativedelta(hours=-int(timedelta), minutes=-int(timedeltaMin))
    else:
        createTime= datetime.datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S") + relativedelta(hours=+int(timedelta), minutes=+int(timedeltaMin))
    return str(createTime)
