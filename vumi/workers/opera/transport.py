from twisted.python import log
from twisted.web import xmlrpc, http
from twisted.web.resource import Resource
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet import reactor
from datetime import datetime, timedelta
from vumi.webapp.api.models import SentSMS, ReceivedSMS, Keyword
from vumi.webapp.api.gateways.opera import utils
from vumi.utils import safe_routing_key
from vumi.webapp.api import forms
from vumi.service import Worker, Consumer, Publisher, JSONEncoder
import cgi, json, iso8601

class OperaHealthResource(Resource):
    isLeaf = True
    def render_GET(self, request):
        request.setResponseCode(http.OK)
        return "OK"

class OperaReceiptResource(Resource):
    
    def __init__(self, publisher):
        self.publisher = publisher
        Resource.__init__(self)
    
    def render_POST(self, request):
        receipts = utils.parse_receipts_xml(request.content.read())
        data = []
        for receipt in receipts:
            dictionary = {
                'transport_name': 'Opera',
                'transport_msg_id': receipt.reference,
                'transport_status': receipt.status,
                'transport_delivered_at': datetime.strptime(
                    receipt.timestamp, 
                    utils.OPERA_TIMESTAMP_FORMAT
                )
            }
            self.publisher.publish_json(dictionary, 
                                        routing_key='sms.receipt.opera')
            data.append(dictionary)
        
        request.setResponseCode(http.ACCEPTED)
        request.setHeader('Content-Type', 'application/json; charset-utf-8')
        return json.dumps(data, cls=JSONEncoder)

class OperaReceiveResource(Resource):
    
    def __init__(self, publisher):
        self.publisher = publisher
        Resource.__init__(self)
    
    def render_POST(self, request):
        content = request.content.read()
        sms = utils.parse_post_event_xml(content)
        # FIXME: this really shouldn't be in the transport
        #
        # update the POST to have the `_from` key copied from `from`. 
        # The model has `_from` defined because `from` is a protected python
        # statement
        
        try:
            head = sms['Text'].split()[0]
            keyword = Keyword.objects.get(keyword=head.lower())
            form = forms.ReceivedSMSForm({
                'user': keyword.user.pk,
                'to_msisdn': sms['Local'],
                'from_msisdn': sms['Remote'],
                'message': sms['Text'],
                'transport_name': 'Opera',
                'received_at': iso8601.parse_date(sms['ReceiveDate'])
            })
            if not form.is_valid():
                raise FormValidationError(form)
            
            receive_sms = form.save()
            log.msg('Receiving an SMS from: %s' % receive_sms.from_msisdn)
            
            # FIXME: signals are going to break things here, all this 
            # shouldn't be in the transport
            # signals.sms_received.send(sender=ReceivedSMS, instance=receive_sms, 
            #                             pk=receive_sms.pk)
            
            # return the response we got back to Opera, it could be re-routed
            # to other services in a callback chain.
            request.setResponseCode(http.OK)
            request.setHeader('Content-Type', 'text/xml; charset=utf8')
            return content
        except Keyword.DoesNotExist, e:
            log.msg("SMS delivered by Opera: %s" % content)
            log.msg("Couldn't find keyword for message: %s" % sms['Text'])
            log.err()
            

class OperaConsumer(Consumer):
    exchange_name = "vumi"
    exchange_type = "direct"
    durable = True
    queue_name = routing_key = "sms.outbound.opera"
    
    def __init__(self, publisher, config):
        self.publisher = publisher
        self.proxy = xmlrpc.Proxy(config.get('url'))
        self.default_values = {
            'Service': config.get('service'),
            'Password': config.get('password'), 
            'Channel': config.get('channel'),
        }
    
    @inlineCallbacks
    def consume_json(self, json):
        dictionary = self.default_values.copy()
        dictionary.update(json)
        
        delivery = dictionary.get('deliver_at', datetime.utcnow())
        expiry = dictionary.get('expire_at', (delivery + timedelta(days=1)))
        
        log.msg("Consumed JSON %s" % dictionary)
        
        sent_sms = SentSMS.objects.get(pk=dictionary['id'])
        
        dictionary['Numbers'] = dictionary.get('to_msisdn')
        dictionary['SMSText'] = dictionary.get('message')
        dictionary['Delivery'] = delivery
        dictionary['Expiry'] = expiry
        dictionary['Priority'] = dictionary.get('priority', 'standard')
        dictionary['Receipt'] = dictionary.get('receipt', 'Y')
        
        proxy_response = yield self.proxy.callRemote('EAPIGateway.SendSMS', dictionary)
        
        sent_sms.transport_msg_id = proxy_response.get('Identifier')
        sent_sms.save()
        returnValue(sent_sms)
    

class OperaPublisher(Publisher):
    exchange_name = "vumi"
    exchange_type = "direct"
    routing_key = "sms.inbound.opera.fallback"
    durable = True
    auto_delete = False
    delivery_mode = 2 # save to disk
    
    def publish_json(self, dictionary, **kwargs):
        log.msg("Publishing JSON %s" % dictionary)
        super(OperaPublisher, self).publish_json(dictionary, **kwargs)
    

class OperaTransport(Worker):
    
    # inlineCallbacks, TwistedMatrix's fancy way of allowing you to write
    # asynchronous code as if it was synchronous by the nifty use of
    # coroutines.
    # See: http://twistedmatrix.com/documents/10.0.0/api/twisted.internet.defer.html#inlineCallbacks
    @inlineCallbacks
    def startWorker(self):
        log.msg("Starting the OperaTransport config: %s" % self.config)
        # create the publisher
        self.publisher = yield self.start_publisher(OperaPublisher)
        # when it's done, create the consumer and pass it the publisher
        self.consumer = yield self.start_consumer(OperaConsumer, self.publisher, self.config)
        
        # start receipt web resource
        self.receipt_resource = yield self.start_web_resources(
            [
                (OperaReceiptResource(self.publisher), self.config['web_receipt_path']),
                (OperaReceiveResource(self.publisher), self.config['web_receive_path']),
                (OperaHealthResource(), 'health'),
            ],
            self.config['web_port']
        )
    
    def stopWorker(self):
        log.msg("Stopping the OperaTransport")
    


