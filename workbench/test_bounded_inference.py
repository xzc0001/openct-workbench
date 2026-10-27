"""Differential voxel/geometry tests against the installed upstream functions."""
from pathlib import Path
import tempfile
import unittest
import numpy as np
import nibabel as nib
from bounded_inference import adapted, compact_undo_crop, native


class BoundedInferenceTests(unittest.TestCase):
    def test_nearest_labels_and_ct_interpolation_identical_to_upstream(self):
        from totalsegmentator.resampling import change_spacing
        # Clone retains the same resampling implementation and numerical order.
        original=change_spacing
        optimized=adapted(original,spacing=True)
        rng=np.random.default_rng(928)
        for dtype,order in [(np.uint8,0),(np.int16,0),(np.int16,3),(np.float32,3)]:
            data=rng.integers(0,120,(21,27,19)).astype(dtype)
            if dtype==np.int16:data-=1000
            affine=np.diag([-.71,.83,1.21,1]);affine[:3,3]=[4,-21,80]
            for shape in [(35,18,31),(13,16,11)]:
                a=nib.Nifti1Image(data,affine);b=nib.Nifti1Image(data.copy(),affine)
                expected=original(a,target_shape=shape,order=order,dtype=dtype,nr_cpus=1)
                actual=optimized(b,target_shape=shape,order=order,dtype=dtype,nr_cpus=1)
                np.testing.assert_array_equal(actual.dataobj,expected.dataobj)
                np.testing.assert_array_equal(actual.affine,expected.affine)
    def test_native_scaled_data_and_crop_match_upstream(self):
        import totalsegmentator.cropping as cropping
        bbox=[[2,16],[3,17],[1,18]]
        data=np.arange(20**3,dtype=np.int16).reshape(20,20,20)-1000
        image=nib.Nifti1Image(data,np.diag([.7,.8,1.2,1]))
        original=cropping.crop_to_bbox_nifti
        optimized=adapted(original,['image'])
        for dtype in [None,np.int32]:
            a=original(image,bbox,dtype);b=optimized(image,bbox,dtype)
            np.testing.assert_array_equal(a.dataobj,b.dataobj)
            np.testing.assert_array_equal(a.affine,b.affine)
        mask=nib.Nifti1Image((data[2:16,3:17,1:18]%3==0).astype(np.uint8),image.affine)
        old=cropping.undo_crop(mask,image,bbox);new=compact_undo_crop(mask,image,bbox)
        np.testing.assert_array_equal(old.dataobj,new.dataobj)
        self.assertEqual(new.dataobj.nbytes*8,old.dataobj.nbytes)
        with tempfile.TemporaryDirectory() as tmp:
            image.header.set_slope_inter(.5,-1024)
            path=Path(tmp)/'scaled.nii.gz';nib.save(image,path)
            loaded=nib.load(path)
            np.testing.assert_array_equal(native(loaded),loaded.get_fdata())
    def test_label_metadata_avoids_float_cache_and_preserves_extension(self):
        from totalsegmentator.nifti_ext_header import add_label_map_to_nifti
        optimized=adapted(add_label_map_to_nifti,['img_in'])
        data=np.zeros((18,19,20),np.uint8);data[3:5]=12
        old=nib.Nifti1Image(data.copy(),np.eye(4));new=nib.Nifti1Image(data.copy(),np.eye(4))
        a=add_label_map_to_nifti(old,{12:'heart'});b=optimized(new,{12:'heart'})
        np.testing.assert_array_equal(a.dataobj,b.dataobj)
        self.assertEqual(a.header.extensions[0].get_content(),b.header.extensions[0].get_content())
        self.assertIsNotNone(a._fdata_cache)
        self.assertIsNone(b._fdata_cache)
    def test_pinned_runtime_adapters_install(self):
        from bounded_inference import install
        import torch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);model=root/'weights/Dataset298_Test/fold_0/checkpoint_final.pth'
            model.parent.mkdir(parents=True);model.write_bytes(b'fixed-runtime-test')
            api=install(2,root/'localizer','ct-sha',root/'weights')
        self.assertTrue(api._workbench_bounded)
        import totalsegmentator.nnunet as nnunet
        self.assertIs(api.nnUNet_predict_image,nnunet.nnUNet_predict_image)
        self.assertEqual(nnunet.nnUNet_predict_image.__name__,'predict_cached')
        # Exercise the real CPU setup before the first model-loading call.
        from unittest.mock import patch
        with patch.dict(nnunet.nnUNetv2_predict.__globals__,{'get_output_folder':lambda *a:'missing-model'}):
            try:nnunet.nnUNetv2_predict('/missing','/missing',298,device='cpu')
            except (FileNotFoundError,RuntimeError):pass
        self.assertLessEqual(torch.get_num_threads(),2)
    def test_localizer_cache_requires_identical_ct_model_and_inference(self):
        from bounded_inference import cached_localizer
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);checkpoint=root/'weights/Dataset298_Test/fold_0/checkpoint_final.pth'
            checkpoint.parent.mkdir(parents=True);checkpoint.write_bytes(b'fixed-model')
            source=root/'ct.nii.gz';image=nib.Nifti1Image(np.ones((12,13,14),np.uint8),np.eye(4));nib.save(image,source)
            calls=[]
            def predict(*args,**kwargs):calls.append(kwargs);return image,nib.load(source),None
            wrapper=cached_localizer(predict,root/'cache','source-sha',root/'weights')
            a=wrapper(source,None,298,resample=6.);b=wrapper(source,None,298,resample=6.)
            self.assertEqual(len(calls),1);np.testing.assert_array_equal(a[0].dataobj,b[0].dataobj)
            wrapper(source,None,298,resample=3.);self.assertEqual(len(calls),2)
            changed=cached_localizer(predict,root/'cache','different-ct',root/'weights')
            changed(source,None,298,resample=6.);self.assertEqual(len(calls),3)
            checkpoint.write_bytes(b'changed-model')
            changed=cached_localizer(predict,root/'cache','source-sha',root/'weights')
            changed(source,None,298,resample=6.);self.assertEqual(len(calls),4)


if __name__=='__main__':unittest.main()
