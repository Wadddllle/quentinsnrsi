import numpy as np

# Load your existing .npy files (or use existing arrays)
array1 = np.load('dose_ar41.npy')

# Compress them together into an .npz file
np.savez_compressed('dose_ar41.npz', name1=array1)
