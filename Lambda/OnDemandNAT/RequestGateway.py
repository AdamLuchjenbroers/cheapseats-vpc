import json
import os
import boto3
import jmespath
import random
from datetime import datetime, timedelta
from dateutil import parser

from botocore.exceptions import ClientError

ec2 = boto3.client('ec2')

def ec2_need_natgw():
    # Returns a True if any currently running instances have
    # The 'NAT-Required' tag set.
    filters = [
        {'Name': 'tag:NAT-Required', 'Values' : ['True','Yes']},
        {'Name': 'instance-state-name', 'Values' : ['running']},
        {'Name': 'vpc-id' : 'Values' : [ os.environ['VPC_ID'] ]}
    ]
    instances = ec2.describe_instances(Filters=filters)
    
    if (len(instances['Reservations']) > 0):
        print("Checking if NAT Gateway is required - YES\n")
        return True
    else:
        print("Checking if NAT Gateway is required - NO\n")
        return False
        
def vpc_has_natgw():
    filters = [
        {'Name': 'tag:OnDemandNAT', 'Values' : ['True','Yes']},
        {'Name': 'state', 'Values' : ['pending', 'available']},
        {'Name': 'vpc-id' : 'Values' : [ os.environ['VPC_ID'] ]}
    ]
    
    gateway_json = ec2.describe_nat_gateways(Filters=filters)
    gateways = jmespath.search('NatGateways[*].[NatGatewayId, State, CreateTime, Tags[?Key==\'LastRequested\'].Value | [0]]', gateway_json)
    
    if len(gateways) > 0:
        print("Checking for existing gateways, found ---\n%s\n---\n" % gateways)
        return gateways
    else:
        print("Checking for existing gateways, found none\n")
        return None
    
def create_nat_gateway():
    alloc_json = ec2.describe_addresses(Filters=[{'Name' : 'tag:Name', 'Values' : ['OnDemandNAT-IPAddr']}])
    allocId = jmespath.search('Addresses[0].AllocationId', alloc_json )
    
    subnet_json = ec2.describe_subnets(Filters=[{'Name' : 'tag:Public', 'Values' : ['Yes']}])
    subnet_list = jmespath.search('Subnets[*].SubnetId', subnet_json)
    subnetId = random.choice(subnet_list)
    
    new_gw_json = ec2.create_nat_gateway(AllocationId=allocId, SubnetId=subnetId)
    gatewayId = jmespath.search('NatGateway.NatGatewayId' , new_gw_json)
    
    print('NAT Gateway Created\n\tID: %s\tInfo ---\n%s\n---\n' % (gatewayId, new_gw_json))
    
    ec2.create_tags(
      Resources=[gatewayId]
    , Tags=[
        {'Key' : 'OnDemandNAT', 'Value' : 'True'}
      , {'Key' : 'Name', 'Value' : 'OnDemandNAT-Gateway'}
      , {'Key' : 'LastRequested', 'Value' : '%s' % datetime.utcnow()}
      , {'Key' : 'Project', 'Value' : 'Deimos-Infra'}
      , {'Key' : 'UseCase', 'Value' : 'AWS-Admin'}
      , {'Key' : 'Application', 'Value' : 'OnDemandNAT'}
      , {'Key' : 'Environment', 'Value' : 'Infrastructure'}
      ]
    )
    return gatewayId
    
def update_route_tables(gatewayId):
    routes_json = ec2.describe_route_tables(Filters=[{'Name' : 'tag:OnDemandNAT', 'Values' : ['Yes', 'True']}])
    routes_list = jmespath.search('RouteTables[*].RouteTableId', routes_json)
    
    print("Fetched Routes List for update ---\n%s\n---\n" % routes_list)
    
    # Wait for gateway to finish starting.
    waiter = ec2.get_waiter('nat_gateway_available')
    waiter.wait(NatGatewayIds = [gatewayId])
    
    for routeTableId in routes_list:
        print("Updating Route Table %s" % routeTableId)
        try:
            ec2.delete_route(RouteTableId = routeTableId, DestinationCidrBlock = '0.0.0.0/0')
        except ClientError as e:
            # We expect the occasional failure where the route doesn't exist - this can be safely ignored.
            if e.response['Error']['Code'] != 'InvalidRoute.NotFound':
                raise e
        ec2.create_route(RouteTableId = routeTableId, DestinationCidrBlock = '0.0.0.0/0', NatGatewayId = gatewayId)
        
        print("Update Completed for %s\n" % routeTableId)
    
def autolaunch_handler(event, context):
    
    nat_needed = ec2_need_natgw()
    gateway_list = vpc_has_natgw()
    
    info = {
        'nat_needed' : nat_needed
    }
    
    if gateway_list == None and nat_needed == True:
        gatewayId = create_nat_gateway()
        update_route_tables(gatewayId)
        
        info['nat-launched'] = gatewayId
    elif gateway_list != None and nat_needed == True:
        for (gatewayId, state, created, lastRequested) in gateway_list:
            ec2.create_tags(
              Resources=[gatewayId]
            , Tags=[ {'Key' : 'LastRequested', 'Value' : '%s' % datetime.utcnow() } ]
           )
    elif gateway_list != None and nat_needed == False:
        gw_change_list = []
        
        for (gatewayId, state, created, lastRequested) in  gateway_list:
            age = datetime.now(created.tzinfo) - created
            
            if lastRequested != None:
                inactive = datetime.utcnow() - parser.isoparse(lastRequested)
            else:
                inactive = age
            
            if inactive >= timedelta(minutes=45):
                ec2.delete_nat_gateway(NatGatewayId = gatewayId)
                gw_change_list.append({'action' : 'deleted', 'gatewayId' : gatewayId, 'age' : ('%s' % age), 'inactive' : ('%s' % inactive)})
            else:
                gw_change_list.append({'action' : 'skipped', 'gatewayId' : gatewayId, 'age' : ('%s' % age), 'inactive' : ('%s' % inactive)})
        info['nat-changed'] = gw_change_list
        
    print("SUMMARY:\n%s\n" % json.dumps(info))
    return info

    
def request_gateway_handler(event, context):
    print("NAT Gateway Requested\n")
    try:
        gateway_list = vpc_has_natgw()
    
        info = {
            'nat_needed' : 'requested'
        }
    
        if gateway_list == None: #and nat_needed == True:
            print("New Gateway Required, Launching\n")
            gatewayId = create_nat_gateway()
            update_route_tables(gatewayId)
            info['nat-launched'] = gatewayId
        else: 
            print("NAT Gateway already provisioned - updating Last Requested Timestamp\n")
            info['nat-existing'] = True
            for (gatewayId, state, created, lastRequested) in gateway_list:
                ec2.create_tags(
                  Resources=[gatewayId]
                , Tags=[ {'Key' : 'LastRequested', 'Value' : '%s' % datetime.utcnow() } ]
               )

        if 'CodePipeline.job' in event:
            job = event['CodePipeline.job']
        
            cp = boto3.client('codepipeline')
            cp.put_job_success_result(
              jobId=job['id']
            )
        
        print("SUMMARY:\n%s\n" % json.dumps(info))
        return info        
    except Exception as e:
        if 'CodePipeline.job' in event:
            job = event['CodePipeline.job']
        
            cp = boto3.client('codepipeline')
            cp.put_job_failure_result(
              jobId=job['id'],
              failureDetails= {
                 "type" : "JobFailed"
              ,  "message" : '%s' % e
              }
            )
        raise e

def check_gateway_required(event, context):
    nat_needed = ec2_need_natgw()
    gateway_list = vpc_has_natgw()
    
    info = {
        'nat_needed' : nat_needed
    }
    
    if gateway_list == None:
        # Nothing to do.
        print("No Gateway running, nothing to do")
        return

    if nat_needed == True:
        print("Gateway still in use - updating last-requested details")
        
        for (gatewayId, state, created, lastRequested) in gateway_list:
            ec2.create_tags(
              Resources=[gatewayId]
            , Tags=[ {'Key' : 'LastRequested', 'Value' : '%s' % datetime.utcnow() } ]
           )
    elif nat_needed == False:
        gw_change_list = []
        
        print("No gateway user detected, checking gateway ages")
        for (gatewayId, state, created, lastRequested) in  gateway_list:
            age = datetime.now(created.tzinfo) - created
            
            if lastRequested != None:
                inactive = datetime.utcnow() - parser.isoparse(lastRequested)
            else:
                inactive = age
            
            if inactive >= timedelta(minutes=45):
                ec2.delete_nat_gateway(NatGatewayId = gatewayId)
                gw_change_list.append({'action' : 'deleted', 'gatewayId' : gatewayId, 'age' : ('%s' % age), 'inactive' : ('%s' % inactive)})
            else:
                gw_change_list.append({'action' : 'skipped', 'gatewayId' : gatewayId, 'age' : ('%s' % age), 'inactive' : ('%s' % inactive)})
        info['nat-changed'] = gw_change_list
        
    print("SUMMARY:\n%s\n" % json.dumps(info))
    return info
