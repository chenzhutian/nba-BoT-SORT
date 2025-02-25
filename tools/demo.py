import sys
import argparse
import os
import os.path as osp
import time
import json
from typing import List, Dict, Tuple
import cv2
import numpy as np
from numpy import number
import torch
from transformers import ViTForImageClassification, ViTFeatureExtractor
import traceback

from loguru import logger

sys.path.append('/home/jerry/nba-BoT-SORT')
sys.path.remove('/home/ztchen/BoT-SORT')
print(sys.path)

from predictor import classify_player
from yolox.data.data_augment import preproc
from yolox.exp import get_exp
from yolox.utils import fuse_model, get_model_info, postprocess
from yolox.utils.visualize import plot_tracking
from tracker.bot_sort import BoTSORT
from tracker.tracking_utils.timer import Timer

IMAGE_EXT = [".jpg", ".jpeg", ".webp", ".bmp", ".png"]


def make_parser():
    parser = argparse.ArgumentParser("BoT-SORT Demo!")
    parser.add_argument("demo", default="image", help="demo type, eg. image, video and webcam")
    parser.add_argument("-expn", "--experiment-name", type=str, default=None)
    parser.add_argument("-n", "--name", type=str, default=None, help="model name")
    parser.add_argument("--path", default="", help="path to images or video")
    parser.add_argument("--camid", type=int, default=0, help="webcam demo camera id")
    parser.add_argument("--save_result", action="store_true",help="whether to save the inference result of image/video")
    parser.add_argument("-f", "--exp_file", default=None, type=str, help="pls input your expriment description file")
    parser.add_argument("-c", "--ckpt", default=None, type=str, help="ckpt for eval")
    parser.add_argument("--device", default="gpu", type=str, help="device to run our model, can either be cpu or gpu")
    parser.add_argument("--conf", default=None, type=float, help="test conf")
    parser.add_argument("--nms", default=None, type=float, help="test nms threshold")
    parser.add_argument("--tsize", default=None, type=int, help="test img size")
    parser.add_argument("--fps", default=30, type=int, help="frame rate (fps)")
    parser.add_argument("--fp16", dest="fp16", default=False, action="store_true",help="Adopting mix precision evaluating.")
    parser.add_argument("--fuse", dest="fuse", default=False, action="store_true", help="Fuse conv and bn for testing.")
    parser.add_argument("--trt", dest="trt", default=False, action="store_true", help="Using TensorRT model for testing.")

    # Gt bbox
    parser.add_argument("-g", "--gt_bbox", default=None, type=str, help="provide the GT bboxes")
    parser.add_argument("--out", default=None, type=str, help="the root folder to output results")
    parser.add_argument("-cls", default=None, type=str, help="weight files for the classifier")

    # tracking args
    parser.add_argument("--track_high_thresh", type=float, default=0.2, help="tracking confidence threshold")
    parser.add_argument("--track_low_thresh", default=0.1, type=float, help="lowest detection threshold")
    parser.add_argument("--new_track_thresh", default=0.7, type=float, help="new track thresh")
    parser.add_argument("--track_buffer", type=int, default=30, help="the frames for keep lost tracks")
    parser.add_argument("--match_thresh", type=float, default=0.8, help="matching threshold for tracking")
    parser.add_argument("--aspect_ratio_thresh", type=float, default=1.6, help="threshold for filtering out boxes of which aspect ratio are above the given value.")
    parser.add_argument('--min_box_area', type=float, default=10, help='filter out tiny boxes')
    parser.add_argument("--fuse-score", dest="fuse_score", default=False, action="store_true", help="fuse score and iou for association")

    # CMC
    parser.add_argument("--cmc-method", default="orb", type=str, help="cmc method: files (Vidstab GMC) | orb | ecc")

    # ReID
    parser.add_argument("--with-reid", dest="with_reid", default=False, action="store_true", help="test mot20.")
    parser.add_argument("--fast-reid-config", dest="fast_reid_config", default=r"fast_reid/configs/MOT17/sbs_S50.yml", type=str, help="reid config file path")
    parser.add_argument("--fast-reid-weights", dest="fast_reid_weights", default=r"pretrained/mot17_sbs_S50.pth", type=str,help="reid config file path")
    parser.add_argument('--proximity_thresh', type=float, default=0.5, help='threshold for rejecting low overlap reid matches')
    parser.add_argument('--appearance_thresh', type=float, default=0.25, help='threshold for rejecting low appearance similarity reid matches')
    return parser


def get_image_list(path):
    image_names = []
    for maindir, subdir, file_name_list in os.walk(path):
        for filename in file_name_list:
            apath = osp.join(maindir, filename)
            ext = osp.splitext(apath)[1]
            if ext in IMAGE_EXT:
                image_names.append(apath)
    return image_names


def write_results(filename, results):
    save_format = '{frame},{id},{x1},{y1},{w},{h},{s},-1,-1,-1\n'
    with open(filename, 'w') as f:
        for frame_id, tlwhs, track_ids, scores in results:
            for tlwh, track_id, score in zip(tlwhs, track_ids, scores):
                if track_id < 0:
                    continue
                x1, y1, w, h = tlwh
                line = save_format.format(frame=frame_id, id=track_id, x1=round(x1, 1), y1=round(y1, 1), w=round(w, 1), h=round(h, 1), s=round(score, 2))
                f.write(line)
    logger.info('save results to {}'.format(filename))


class Predictor(object):
    def __init__(
        self,
        model,
        exp,
        trt_file=None,
        decoder=None,
        device=torch.device("cpu"),
        fp16=False
    ):
        self.model = model
        self.decoder = decoder
        self.num_classes = exp.num_classes
        self.confthre = exp.test_conf
        self.nmsthre = exp.nmsthre
        self.test_size = exp.test_size
        self.device = device
        self.fp16 = fp16
        if trt_file is not None:
            from torch2trt import TRTModule

            model_trt = TRTModule()
            model_trt.load_state_dict(torch.load(trt_file))

            x = torch.ones((1, 3, exp.test_size[0], exp.test_size[1]), device=device)
            self.model(x)
            self.model = model_trt
        self.rgb_means = (0.485, 0.456, 0.406)
        self.std = (0.229, 0.224, 0.225)

    def inference(self, img, timer):
        img_info = {"id": 0}
        if isinstance(img, str):
            img_info["file_name"] = osp.basename(img)
            img = cv2.imread(img)
        else:
            img_info["file_name"] = None

        height, width = img.shape[:2]
        img_info["height"] = height
        img_info["width"] = width
        img_info["raw_img"] = img

        img, ratio = preproc(img, self.test_size, self.rgb_means, self.std)
        img_info["ratio"] = ratio
        img = torch.from_numpy(img).unsqueeze(0).float().to(self.device)
        if self.fp16:
            img = img.half()  # to FP16

        with torch.no_grad():
            timer.tic()
            outputs = self.model(img)
            if self.decoder is not None:
                outputs = self.decoder(outputs, dtype=outputs.type())
            outputs = postprocess(outputs, self.num_classes, self.confthre, self.nmsthre)
        return outputs, img_info


def image_demo(predictor, vis_folder, current_time, args):
    if osp.isdir(args.path):
        files = get_image_list(args.path)
    else:
        files = [args.path]
    files.sort()

    tracker = BoTSORT(args, frame_rate=args.fps)

    timer = Timer()
    results = []

    for frame_id, img_path in enumerate(files, 1):

        # Detect objects
        outputs, img_info = predictor.inference(img_path, timer)
        scale = min(exp.test_size[0] / float(img_info['height'], ), exp.test_size[1] / float(img_info['width']))

        detections = []
        if outputs[0] is not None:
            outputs = outputs[0].cpu().numpy()
            detections = outputs[:, :7]
            detections[:, :4] /= scale

            # Run tracker
            online_targets = tracker.update(detections, img_info['raw_img'])

            online_tlwhs = []
            online_ids = []
            online_scores = []
            for t in online_targets:
                tlwh = t.tlwh
                tid = t.track_id
                vertical = tlwh[2] / tlwh[3] > args.aspect_ratio_thresh
                if tlwh[2] * tlwh[3] > args.min_box_area and not vertical:
                    online_tlwhs.append(tlwh)
                    online_ids.append(tid)
                    online_scores.append(t.score)
                    # save results
                    results.append(
                        f"{frame_id},{tid},{tlwh[0]:.2f},{tlwh[1]:.2f},{tlwh[2]:.2f},{tlwh[3]:.2f},{t.score:.2f},-1,-1,-1\n"
                    )
            timer.toc()
            online_im = plot_tracking(
                img_info['raw_img'], online_tlwhs, online_ids, frame_id=frame_id, fps=1. / timer.average_time
            )
        else:
            timer.toc()
            online_im = img_info['raw_img']

        # result_image = predictor.visual(outputs[0], img_info, predictor.confthre)
        if args.save_result:
            timestamp = time.strftime("%Y_%m_%d_%H_%M_%S", current_time)
            save_folder = osp.join(vis_folder, timestamp)
            os.makedirs(save_folder, exist_ok=True)
            cv2.imwrite(osp.join(save_folder, osp.basename(img_path)), online_im)

        if frame_id % 20 == 0:
            logger.info('Processing frame {} ({:.2f} fps)'.format(frame_id, 1. / max(1e-5, timer.average_time)))

        ch = cv2.waitKey(0)
        if ch == 27 or ch == ord("q") or ch == ord("Q"):
            break

    if args.save_result:
        res_file = osp.join(vis_folder, f"{timestamp}.txt")
        with open(res_file, 'w') as f:
            f.writelines(results)
        logger.info(f"save results to {res_file}")


def imageflow_demo(predictor, vis_folder, gt_bboxes: Dict[int, 
                                                          Tuple[ 
                                                               List[List[number]], 
                                                               List[int]
                                                               ]  
                                                          ], current_time, args):
    cap = cv2.VideoCapture(args.path if args.demo == "video" else args.camid)
    width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)  # float
    height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)  # float
    fps = cap.get(cv2.CAP_PROP_FPS)
    # timestamp = time.strftime("%Y_%m_%d_%H_%M_%S", current_time)

    save_folder = osp.join(args.out, osp.basename(args.path)[:-len('.mp4')])
    os.makedirs(save_folder, exist_ok=True)
    save_path = osp.join(save_folder, f"{osp.basename(args.path)[:-len('.mp4')]}_tracking.mp4")
    logger.info(f"video save_path is {save_path}")

    vid_writer = cv2.VideoWriter(
        save_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (int(width), int(height))
    )
    tracker = BoTSORT(args, frame_rate=args.fps)
    timer = Timer()
    # frame_id = 0
    
    #
    model_name_or_path = 'google/vit-base-patch16-224-in21k'
    feature_extractor = ViTFeatureExtractor.from_pretrained(model_name_or_path)
    # classifier = ViTForImageClassification.from_pretrained('/datadrive/player-classifier/game1-classifier/')
    classifier = ViTForImageClassification.from_pretrained(args.cls)
    classifier.eval().cuda()
    
    ims_to_process = []
    while True:
        ret_val, frame = cap.read()
        if ret_val:
            ims_to_process.append(frame)
        else:
            break
    
    results = []
    ims_to_write = []
    for frame_id, frame in enumerate(ims_to_process):
    
    # while True:
        if frame_id % 20 == 0:
            logger.info('Processing frame {} ({:.2f} fps)'.format(frame_id, 1. / max(1e-5, timer.average_time)))
        # ret_val, frame = cap.read()
        # if ret_val:
        detections = None
        gt_ids = None
        # if frame with GT
        if frame_id in gt_bboxes and False:
            detections, gt_ids = gt_bboxes[frame_id]
            img_info = { "raw_img": frame }
        else:
            # Detect objects
            outputs, img_info = predictor.inference(frame, timer)
            scale = min(exp.test_size[0] / float(img_info['height'], ), exp.test_size[1] / float(img_info['width']))

            if outputs[0] is not None:
                outputs = outputs[0].cpu().numpy()
                detections = outputs[:, :7]
                detections[:, :4] /= scale
                
                # do classification
                if detections.shape[1] == 5:
                    scores = detections[:, 4]
                    bboxes = detections[:, :4]
                    classes = detections[:, -1]
                else:
                    scores = detections[:, 4] * detections[:, 5]
                    bboxes = detections[:, :4]  # x1y1x2y2
                    classes = detections[:, -1]
                
                lowest_inds = scores > tracker.track_low_thresh
                bboxes = bboxes[lowest_inds]
                scores = scores[lowest_inds]
                classes = classes[lowest_inds]

                # player_inds = []
                patches = []
                for bIdx, bbox in enumerate(bboxes):                            
                    # if bbox.min() < 0 or bbox.max() > 1280: continue
                    x1, y1, x2, y2 = bbox.astype(int)
                    x1, x2 = np.clip([x1, x2], 0, 1280)
                    y1, y2 = np.clip([y1, y2], 0, 720)
                    try:
                        patch = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)          
                        patches.append((bIdx, patch))
                    except:
                        print(x1, y1, x2, y2)
                
                # if frame_id == 48:
                #     images = [cv2.cvtColor(patch, cv2.COLOR_RGB2BGR) for _, patch in patches if patch.shape[0] > 1]
                #     [cv2.imwrite(f'48/{i}.png', img) for (i, img) in enumerate(images)]
                #     breakpoint()
                # if frame_id == 11:
                #     bbox_vis = frame.copy()
                #     for bIdx, bbox in enumerate(bboxes):
                #         x1, y1, x2, y2 = bbox.astype(int)
                #         cv2.rectangle(bbox_vis, [x1, y1], [x2, y2], color=[255,0,0], thickness=2)
                #     cv2.imwrite('frame11.png', bbox_vis)
                #     breakpoint()
                try:
                    inputs = feature_extractor([patch for _, patch in patches if patch.shape[0] > 1], return_tensors="pt")
                except Exception as e:
                    print(e)
                    print(traceback.format_exc())
                    print(sys.exc_info()[2])
                    for _, patch in patches:
                        print(patch.shape)
                    return            
                inputs['pixel_values'] = inputs['pixel_values'].cuda()
                # print(x1, y1, x2, y2)
                with torch.no_grad():
                    logits = classifier(**inputs).logits
                    
                tmp = (logits.argmax(-1) == 1).nonzero().squeeze().tolist()
                player_inds = [patches[i][0] for i in tmp] if isinstance(tmp, list) else [patches[tmp][0]]
                # print(frame_id, player_inds)
                # predicted_labels = logits.argmax(-1).cpu().numpy()
                # player_inds = [bIdx for (bIdx, _), label in zip(patches, labels) if label == 1]
                # print('#players', player_inds, len(player_inds))
                detections = detections[player_inds]

                # gt_ids: identify using classifier
                # video_id = args.path.split('/')[-1][:-len('.mp4')]
                # game_id = args.path.split('/')[-3]
                gt_ids = np.array([classify_player(p) for p in [patches[i][1] for i in tmp]])

        # do the tracking
        if detections is not None:
            # Run tracker
            online_targets = tracker.update(detections, gt_ids, img_info["raw_img"])

            online_tlwhs = []
            online_ids = []
            online_scores = []
            for t in online_targets:
                tlwh = t.tlwh
                tid = t.track_id
                # if tid > 9: continue
                vertical = tlwh[2] / tlwh[3] > args.aspect_ratio_thresh
                if tlwh[2] * tlwh[3] > args.min_box_area and not vertical:
                    online_tlwhs.append(tlwh)
                    online_ids.append(tid)
                    online_scores.append(t.score)
                    results.append(
                        f"{frame_id},{tid},{tlwh[0]:.2f},{tlwh[1]:.2f},{tlwh[2]:.2f},{tlwh[3]:.2f},{t.score:.2f},-1,-1,-1\n"
                    )
            timer.toc()
            online_im = plot_tracking(
                img_info['raw_img'], online_tlwhs, online_ids, frame_id=frame_id + 1, fps=1. / timer.average_time
            )
        else:
            timer.toc()
            online_im = img_info['raw_img']

        ims_to_write.append(online_im)
            # ch = cv2.waitKey(1)
            # if ch == 27 or ch == ord("q") or ch == ord("Q"):
            #     break
        # else:
        #     break
        # frame_id += 1

    if args.save_result:
        for online_im in ims_to_write:
            vid_writer.write(online_im)
            
        res_file = osp.join(save_folder, f"{osp.basename(args.path)[:-len('.mp4')]}_tracking.txt")
        with open(res_file, 'w') as f:
            f.writelines(results)
        logger.info(f"save results to {res_file}")

def main(exp, args):
    if not args.experiment_name:
        args.experiment_name = exp.exp_name

    output_dir = osp.join(exp.output_dir, args.experiment_name)
    os.makedirs(output_dir, exist_ok=True)

    if args.save_result:
        vis_folder = osp.join(output_dir, "track_vis")
        os.makedirs(vis_folder, exist_ok=True)

    if args.trt:
        args.device = "gpu"
    args.device = torch.device("cuda" if args.device == "gpu" else "cpu")

    logger.info("Args: {}".format(args))

    if args.conf is not None:
        exp.test_conf = args.conf
    if args.nms is not None:
        exp.nmsthre = args.nms
    if args.tsize is not None:
        exp.test_size = (args.tsize, args.tsize)

    model = exp.get_model().to(args.device)
    logger.info("Model Summary: {}".format(get_model_info(model, exp.test_size)))
    model.eval()

    if not args.trt:
        if args.ckpt is None:
            ckpt_file = osp.join(output_dir, "best_ckpt.pth.tar")
        else:
            ckpt_file = args.ckpt
        logger.info("loading checkpoint")
        ckpt = torch.load(ckpt_file, map_location="cpu")
        # load the model state dict
        model.load_state_dict(ckpt["model"])
        logger.info("loaded checkpoint done.")

    if args.fuse:
        logger.info("\tFusing model...")
        model = fuse_model(model)

    if args.fp16:
        model = model.half()  # to FP16

    if args.trt:
        assert not args.fuse, "TensorRT model is not support model fusing!"
        trt_file = osp.join(output_dir, "model_trt.pth")
        assert osp.exists(
            trt_file
        ), "TensorRT model is not found!\n Run python3 tools/trt.py first!"
        model.head.decode_in_inference = False
        decoder = model.head.decode_outputs
        logger.info("Using TensorRT to inference")
    else:
        trt_file = None
        decoder = None

    gtByFrames = {}
    if args.gt_bbox is not None and os.path.exists(args.gt_bbox):
        # convert to List[bboxes, List[int]]
        with open(args.gt_bbox) as f:
            annot = json.load(f)
        labels = annot['labels']
        infos = annot['info']
        frames_map = [int(u.split('_')[-1].split('.')[0]) for u in infos['url']]
        for pIdx, player in enumerate(labels):
            frames = player['data']['frames']
            #group by frame
            for frame in frames:
                frame_idx = frames_map[frame['frame']]
                if frame_idx not in gtByFrames:
                    gtByFrames[frame_idx] = [[], []]
                gtByFrames[frame_idx][0].append(frame['points'] + [1])
                gtByFrames[frame_idx][1].append(pIdx)
        for k, v in gtByFrames.items():
            gtByFrames[k] = (np.array(v[0]), np.array(v[1]))

    predictor = Predictor(model, exp, trt_file, decoder, args.device, args.fp16)

    current_time = time.localtime()
    if args.demo == "image" or args.demo == "images":
        image_demo(predictor, vis_folder, current_time, args)
    elif args.demo == "video" or args.demo == "webcam":
        imageflow_demo(predictor, vis_folder, gtByFrames, current_time, args)
    else:
        raise ValueError("Error: Unknown source: " + args.demo)


if __name__ == "__main__":
    args = make_parser().parse_args()
    exp = get_exp(args.exp_file, args.name)

    args.ablation = False
    args.mot20 = not args.fuse_score

    main(exp, args)
