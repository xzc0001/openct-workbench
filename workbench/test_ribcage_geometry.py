"""Native-density geometry regressions: restore omitted CT bone, never invent a bridge."""
import unittest
import numpy as np
import SimpleITK as sitk
from scipy import ndimage as ndi
from ribcage_geometry import select_ct_bone, build_ribcage


class RibcageGeometryTests(unittest.TestCase):
    def test_one_marker_restores_real_ct_between_truncated_ai_seeds(self):
        hu=np.full((20,20,40),-1000,np.int16);hu[8:12,8:12,4:36]=700
        positive=np.zeros_like(hu,bool);positive[8:12,8:12,4:10]=1;positive[8:12,8:12,30:36]=1
        selected,receipt=select_ct_bone(hu,positive,np.zeros_like(positive))
        self.assertTrue(selected[8:12,8:12,10:30].all())
        self.assertTrue(np.array_equal(selected,hu>=150))
        self.assertTrue(receipt['rib_and_spine_share_marker'])

    def test_real_low_density_joint_remains_empty(self):
        hu=np.full((20,20,40),-1000,np.int16);hu[8:12,8:12,4:36]=700;hu[:,:,19:22]=40
        positive=np.zeros_like(hu,bool);positive[8:12,8:12,4:10]=1;positive[8:12,8:12,30:36]=1
        selected,receipt=select_ct_bone(hu,positive,np.zeros_like(positive))
        self.assertFalse(selected[:,:,19:22].any())
        self.assertEqual(ndi.label(selected)[1],2)
        self.assertEqual(receipt['synthetic_bone_voxels'],0)

    def test_shoulder_markers_remain_separate_at_ct_contact(self):
        hu=np.full((16,16,40),-1000,np.int16);hu[6:10,6:10,3:37]=700
        positive=np.zeros_like(hu,bool);positive[7,7,5]=1
        negative=np.zeros_like(positive);negative[7,7,35]=1
        selected,_=select_ct_bone(hu,positive,negative)
        self.assertTrue(selected[7,7,5]);self.assertFalse(selected[7,7,35])
        self.assertTrue(np.all(hu[selected]>=150))

    def test_corner_touching_noise_is_not_grown_but_localized_thin_bone_is_kept(self):
        hu=np.full((16,16,24),-1000,np.int16);hu[4:8,4:8,3:14]=700
        hu[8,8,14]=250;hu[9:12,9:12,15:18]=250
        hu[13,13,20]=700
        positive=np.zeros_like(hu,bool);positive[5,5,5]=1;positive[13,13,20]=1
        selected,receipt=select_ct_bone(hu,positive,np.zeros_like(positive))
        self.assertTrue(selected[4:8,4:8,3:14].all())
        self.assertFalse(selected[8:12,8:12,14:18].any())
        self.assertTrue(selected[13,13,20])
        self.assertEqual(receipt['density_connectivity'],6)

    def test_native_gray_surface_rotation_anisotropy_and_source_pixels(self):
        z,y,x=np.indices((40,40,48));spacing=np.array([.7,.9,1.1]);center=np.array([23.5,19.5,19.5])
        r=np.sqrt(((x-center[0])*spacing[0])**2+((y-center[1])*spacing[1])**2+((z-center[2])*spacing[2])**2)
        hu=(150+(10-r)*35).astype(np.float32);original=hu.copy()
        image=sitk.GetImageFromArray(hu);image.SetSpacing(spacing);image.SetOrigin([20.,-40.,5.]);angle=np.pi/4
        image.SetDirection([np.cos(angle),-np.sin(angle),0.,np.sin(angle),np.cos(angle),0.,0.,0.,1.])
        positive=r<5
        mask,poly,report=build_ribcage(image,image,positive,np.zeros_like(positive))
        from vtk.util.numpy_support import vtk_to_numpy
        vertices=vtk_to_numpy(poly.GetPoints().GetData())
        actual_center=np.array(image.TransformContinuousIndexToPhysicalPoint(tuple(center)))*[-1,-1,1]
        self.assertLess(float(np.abs(np.linalg.norm(vertices-actual_center,axis=1)-10).max()),.1)
        self.assertLess(report['native_ct_surface_audit']['max_interpolated_hu_error'],.05)
        self.assertTrue(np.array_equal(sitk.GetArrayFromImage(image),original))
        self.assertTrue(np.array_equal(mask,hu>=150))
        self.assertFalse(report['artificial_joint_bridges'])


if __name__=='__main__':unittest.main()
