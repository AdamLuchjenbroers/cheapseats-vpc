import json
import os
import boto3

def ec2_need_natgw(ec2):
    # Returns a True if any currently running instances have
    # The 'NAT-Required' tag set.
    filters = [
        {'Name': 'tag:NAT-Required', 'Values' : ['True','Yes']},
        {'Name': 'instance-state-name', 'Values' : ['running',]}
    ]
    instances = ec2.describe_instances(Filters=filters)
    
    if (len(instances['Reservations']) > 0):
        return True
    else:
        return False
        
def vpc_has_natgw(ec2):
    filters = [
        {'Name': 'tag:OnDemandNAT', 'Values' : ['True','Yes']},
    ]
    
    gateways = ec2.describe_nat_gateways(Filters=filters)
    print('%s\n' % gateways)
    
    # Returns true if a VPC has a NAT gateway currently running
    return False #FIXME

def lambda_handler(event, context):
    ec2 = boto3.client('ec2')
    
    nat_needed = ec2_need_natgw(ec2)
    nat_available = vpc_has_natgw(ec2)
    
    return {
        'nat-needed' : nat_needed
    ,   'nat-avail'  : nat_available
    }