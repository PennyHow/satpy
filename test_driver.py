# -*- coding: utf-8 -*-
"""
Created on Wed Jul  8 11:58:05 2020

@author: HOW
"""

import numpy as np
import matplotlib.pyplot as plt
import os,sys
from pyhdf.SD import SD, SDC

#------------------------------------------------------------------------------

def getBitValue(bit_start, bit_count, value):
    '''Return bit value at specific data position.
    Args
    bit_start (int):            Bit starting position
    bit_count (int):            Number of bits
    value (int):                Byte data at single point position
    
    Returns
    Bit value
    '''
    bitmask=pow(2, bit_start + bit_count) -1
    return np.right_shift(np.bitwise_and(value,bitmask), bit_start)


def getBitLayer(data, byte, bit, bit_number):
    '''
    Return bit data for entire data layer.
    Args
    data (arr):         Data loaded from hdf file, 3D array. Check array 
                        dimensions. If not (bytes, width, height) then change 
                        data input into getBitValue function
    byte (int):         Byte nposition
    bit (int):          Bit position
    bit_number (int):   Number of bits
    
    Returns
    Array containing bit information
    '''
    outlayer=[]
    for i in range(data.shape[1]):
        for j in range(data.shape[2]):
            outlayer.append(getBitValue(bit, bit_number, data[byte,i,j]))
    outlayer = np.reshape(outlayer, (data.shape[1], data.shape[2]))
    return outlayer
 
    
def getBitLayer_NoData(data, byte, bit, bit_number):
    '''
    Return bit data for entire data layer, with nodata cells removed (calculated
    from Byte 1, bit field 0 where cloud mask flag denotes if mask has been
    determined or not)
    Args
    data (arr):         Data loaded from hdf file, 3D array. Check array 
                        dimensions. If not (bytes, width, height) then change 
                        data input into getBitValue function
    byte (int):         Byte nposition
    bit (int):          Bit position
    bit_number (int):   Number of bits
    
    Returns
    Array containing bit information
    '''
    outlayer=[]
    for i in range(data.shape[1]):
        for j in range(data.shape[2]):
            
            #Check if cloud data was computed
            flag1 = getBitValue(0, 1, data[0,i,j])
            if flag1==1:
                
                # #Check if day or night
                # flag2 = getBitValue(3, 1, data[0,i,j])
                # if flag2==1:
                    
                outlayer.append(getBitValue(bit, bit_number, data[byte,i,j]))
            
            # #If flags fail, append nan value
            #     else: outlayer.append(np.nan)
            else:
                outlayer.append(np.nan)
    outlayer = np.reshape(outlayer, (data.shape[1], data.shape[2]))
    return outlayer

#------------------------------------------------------------------------------

#Define file location
filename = "P:/B69_RemoteBasis/data processing/HOW_test/test_data/laads/MOD35_hdf/MOD35_L2.A2020287.0035.061.2020287191609.pscs_000501506716.hdf"

#Read hdf file
file = SD(filename, SDC.READ)
# print(file.info())
datasets_dic = file.datasets()

#Print hdf info
for idx,sds in enumerate(datasets_dic.keys()):
    print(idx,sds)

#Select mask info
sds_obj = file.select('Cloud_Mask')                         #Select sds
data = sds_obj.get()                                        #Get sds data

#Assign offset and scale factor
attributes = sds_obj.attributes()
for key, value in attributes.items():
    print(str(key) + ': ' + str(value))
    if key == 'add_offset':
        add_offset = value  
    if key == 'scale_factor':
        scale_factor = value
#data = (data - add_offset) * scale_factor


#Get general cloud mask
byte=0
bit=1
num_bits=2
cloud = getBitLayer_NoData(data, byte, bit, num_bits)

#Get cirrus cloud (optical)
byte=1
bit=1
num_bits=1
cirrus1 = getBitLayer_NoData(data, byte, bit, num_bits)

#Get cirrus cloud (IR)
byte=1
bit=3
num_bits=1
cirrus2 = getBitLayer_NoData(data, byte, bit, num_bits)

#Plot cloud layers
fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(20,10), sharex=True, sharey=True)
ax1.imshow(cloud)
ax2.imshow(cirrus1)
ax3.imshow(cirrus2)
plt.show()