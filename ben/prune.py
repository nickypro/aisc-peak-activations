import copy
from typing import List, Optional

import einops
import numpy as np
import torch
import wandb

from taker.activations import (choose_attn_heads_by, get_midlayer_activations,
                          get_top_frac, save_timestamped_tensor_dict)
from taker.data_classes import (ActivationOverview, PruningConfig, RunDataHistory,
                           RunDataItem)
from taker.eval import evaluate_all
from taker.model import Model
from taker.scoring import score_indices, score_indices_by
from taker.texts import prepare


def prune_and_evaluate(
        opt: Model,
        pruning_config: PruningConfig,
        focus_out: Optional[dict] = None,
        cripple_out: Optional[dict] = None,
        iteration: Optional[int] = None,
    ):
    """
    Prune and evaluate the model

    Args:
        opt (Model): model to prune and evaluate
        pruning_config (PruningConfig): config for pruning
        focus_out (dict): output of get_midlayer_activations for focus dataset
        cripple_out (dict): output of get_midlayer_activations for cripple dataset
        iteration (int): iteration number for when activations are not recalculated

    Returns:
        output (RunDataItem): Eval data to add to RunDataHistory.
    """
    c = copy.deepcopy(pruning_config)

    # Find out what we are doing
    do_ff   = pruning_config.ff_frac > 0
    do_attn = pruning_config.attn_frac > 0
    if not do_ff and not do_attn:
        raise ValueError("Must prune at least one of FF or Attention")
    if do_attn and pruning_config.attn_mode not in ["pre-out", "value"]:
        raise NotImplementedError("attn_mode must be 'pre-out' or 'value'")

    # Get midlayer activations of FF and ATTN
    if pruning_config.recalculate_activations:
        focus_out   = get_midlayer_activations( opt, pruning_config.focus,
            pruning_config.collection_sample_size, pruning_config.attn_mode )
        cripple_out = get_midlayer_activations( opt, pruning_config.cripple,
            pruning_config.collection_sample_size, pruning_config.attn_mode )

    # Otherwise, import activation data, and adjust the "pruning fraction"
    else:
        c["ff_frac"]   = min( 1.0, c["ff_frac"]*(iteration+1) )
        c["attn_frac"] = min( 1.0, c["attn_frac"]*(iteration+1) )
        assert not (focus_out is None or cripple_out is None or iteration is None), \
            "Must provide focus_out and cripple_out if not recalculate_activations"

    # Prune the model using the activation data
    data = score_and_prune(opt, focus_out, cripple_out, c)

    # Evaluate the model
    with torch.no_grad():
        eval_out = evaluate_all(opt, c.eval_sample_size, c.datasets,
                                dataset_tokens_to_skip=c.collection_sample_size)
        data.update(eval_out)

    return data

import numpy as np  # TODO: move to top if we keep this
from scipy import stats


def get_mean_offsets(activations):
    # Check if the activations tensor is of type torch.float16
    if activations.dtype == torch.float16:
        # Convert to torch.float32 for mode calculation
        activations_float32 = activations.float()
    else:
        # Use the original tensor if it's already in a supported data type
        activations_float32 = activations

    # Compute the mode across the last dimension for each neuron in every layer
    #mode_values_float32, _ = torch.mode(activations_float32, dim=-1)
    mode_values_float32 = torch.mean(activations_float32, dim=-1)
    
    # If the original tensor was torch.float16, convert the result back to torch.float16
    if activations.dtype == torch.float16:
        mode_values = mode_values_float32.half()
    else:
        mode_values = mode_values_float32
    
    # The mode_values tensor will have shape [layers, neurons], which is already 2D
    # and matches the requirement of returning a 2D tensor of mode values.
    
    return mode_values

def get_bucket_peaks(activations):
    
    print('GETTING BUCKETS AT INNER LEVEL')
    # Check if the activations tensor is of type torch.float16
    if activations.dtype == torch.float16:
        # Convert to torch.float32 for histogram calculation
        activations_float32 = activations.float()
    else:
        # Use the original tensor if it's already in a supported data type
        activations_float32 = activations

    # Prepare for histogram computation
    bins = 100
    #min_val = activations_float32.min()
    #max_val = activations_float32.max()

    # Initialize an empty tensor to hold the peak values
    peak_values_float32 = torch.empty(activations_float32.size()[:-1], device=activations_float32.device, dtype=torch.float32)
    
    # Compute the histogram and find the peak for each neuron in every layer
    for i in range(activations_float32.size()[0]):  # Assuming the first dimension is layers
        for j in range(activations_float32.size()[1]):  # Assuming the second dimension is neurons
            min_val = activations_float32[i, j].min()
            max_val = activations_float32[i, j].max()
            
            print(min_val)

            hist = torch.histc(activations_float32[i, j], bins=bins, min=min_val, max=max_val)
            peak_bin = hist.argmax()
            # Compute the center value of the peak bin
            bin_width = (max_val - min_val) / bins
            peak_value = min_val + bin_width * (peak_bin.float() + 0.5)
            peak_values_float32[i, j] = peak_value

    # If the original tensor was torch.float16, convert the result back to torch.float16
    if activations.dtype == torch.float16:
        peak_values = peak_values_float32.half()
    else:
        peak_values = peak_values_float32

    return peak_values

def get_dual_bucket_peaks(activations):
    print('GETTING BUCKETS AT INNER LEVEL')
    # Check if the activations tensor is of type torch.float16
    if activations.dtype == torch.float16:
        # Convert to torch.float32 for histogram calculation
        activations_float32 = activations.float()
    else:
        # Use the original tensor if it's already in a supported data type
        activations_float32 = activations

    # Prepare for histogram computation with both coarse and fine bins
    coarse_bins = 100  # Fewer bins for the coarse filtering step
    fine_bins = 100  # More bins for the detailed peak finding step

    peak_values_float32 = torch.empty(activations_float32.size()[:-1], device=activations_float32.device, dtype=torch.float32)
    
    for i in range(activations_float32.size()[0]):  # Assuming the first dimension is layers
        for j in range(activations_float32.size()[1]):  # Assuming the second dimension is neurons
            min_val = activations_float32[i, j].min()
            max_val = activations_float32[i, j].max()

            # Coarse histogram to filter out noise
            coarse_hist = torch.histc(activations_float32[i, j], bins=coarse_bins, min=min_val, max=max_val)
            coarse_peak_bin = coarse_hist.argmax()
            coarse_bin_width = (max_val - min_val) / coarse_bins
            coarse_peak_min = min_val + coarse_bin_width * coarse_peak_bin
            coarse_peak_max = coarse_peak_min + coarse_bin_width

            # Fine histogram within the identified coarse peak region
            fine_hist = torch.histc(activations_float32[i, j], bins=fine_bins, min=coarse_peak_min, max=coarse_peak_max)
            fine_peak_bin = fine_hist.argmax()
            fine_bin_width = coarse_bin_width / fine_bins
            peak_value = coarse_peak_min + fine_bin_width * (fine_peak_bin.float() + 0.5)
            peak_values_float32[i, j] = peak_value

    # If the original tensor was torch.float16, convert the result back to torch.float16
    if activations.dtype == torch.float16:
        peak_values = peak_values_float32.half()
    else:
        peak_values = peak_values_float32

    return peak_values

def get_averaged_bucket_peaks(activations):
    print('GETTING BUCKETS AT INNER LEVEL')
    # Check if the activations tensor is of type torch.float16
    if activations.dtype == torch.float16:
        # Convert to torch.float32 for histogram calculation
        activations_float32 = activations.float()
    else:
        # Use the original tensor if it's already in a supported data type
        activations_float32 = activations

    # Prepare for histogram computation with both coarse and fine bins
    coarse_bins = 1000  # Coarse resolution
    fine_bins = 10000  # Fine resolution

    peak_values_float32 = torch.empty(activations_float32.size()[:-1], device=activations_float32.device, dtype=torch.float32)
    
    for i in range(activations_float32.size()[0]):  # Assuming the first dimension is layers
        for j in range(activations_float32.size()[1]):  # Assuming the second dimension is neurons
            min_val = activations_float32[i, j].min()
            max_val = activations_float32[i, j].max()

            # Calculate histograms for both resolutions
            coarse_hist = torch.histc(activations_float32[i, j], bins=coarse_bins, min=min_val, max=max_val)
            fine_hist = torch.histc(activations_float32[i, j], bins=fine_bins, min=min_val, max=max_val)

            # Scale coarse histogram to fine resolution for direct comparison
            # This involves expanding each coarse bin count equally among the corresponding set of fine bins
            scale_factor = fine_bins // coarse_bins
            coarse_hist_scaled = coarse_hist.repeat_interleave(scale_factor)

            # In case fine_bins is not a perfect multiple of coarse_bins, adjust the length of the scaled histogram
            if coarse_hist_scaled.size(0) > fine_hist.size(0):
                coarse_hist_scaled = coarse_hist_scaled[:fine_hist.size(0)]

            # Average the histograms
            averaged_hist = (coarse_hist_scaled.float() + fine_hist.float()) / 2

            # Find the peak in the averaged histogram (you might choose another metric to identify the peak)
            peak_bin = averaged_hist.argmax()
            bin_width = (max_val - min_val) / fine_bins
            peak_value = min_val + bin_width * (peak_bin.float() + 0.5)
            peak_values_float32[i, j] = peak_value

    # If the original tensor was torch.float16, convert the result back to torch.float16
    if activations.dtype == torch.float16:
        peak_values = peak_values_float32.half()
    else:
        peak_values = peak_values_float32

    return peak_values


import torch
import torch.nn.functional as F

def apply_gaussian_smoothing(tensor, kernel_size, sigma):
    # Make sure the kernel size is odd to have a valid center position
    if kernel_size % 2 == 0:
        kernel_size += 1
    
    # Create a Gaussian kernel
    x = torch.linspace(-kernel_size // 2, kernel_size // 2, kernel_size)
    gaussian_kernel = torch.exp(-0.5 * (x / sigma).pow(2))
    gaussian_kernel /= gaussian_kernel.sum()
    
    # Add batch and channel dimensions
    gaussian_kernel = gaussian_kernel.view(1, 1, -1).to(tensor.device)
    
    # Manual padding to ensure compatibility with conv1d
    pad_size = kernel_size // 2
    tensor_padded = F.pad(tensor.view(1, 1, -1), (pad_size, pad_size), mode='replicate')
    
    # Apply convolution
    smoothed_tensor = F.conv1d(tensor_padded, gaussian_kernel)
    
    return smoothed_tensor.view(-1)

def get_bucket_peaks_averaged_with_smoothing(activations, kernel_size=5, sigma=1):
    # Your setup code remains unchanged
    
    peak_values_float32 = torch.empty(activations.size()[:-1], device=activations.device, dtype=torch.float32)
    
    for i in range(activations.size()[0]):  # Layers
        for j in range(activations.size()[1]):  # Neurons
            min_val = activations[i, j].min()
            max_val = activations[i, j].max()

            fine_hist = torch.histc(activations[i, j], bins=100, min=min_val, max=max_val)

            # Apply Gaussian smoothing to the fine histogram
            smoothed_hist = apply_gaussian_smoothing(fine_hist, kernel_size, sigma)

            peak_bin = smoothed_hist.argmax()
            bin_width = (max_val - min_val) / 100
            peak_value = min_val + bin_width * (peak_bin.float() + 0.5)
            peak_values_float32[i, j] = peak_value

    # Conversion and return logic remains the same
    return peak_values_float32

def get_global_min_max(activations):
    """Get the global minimum and maximum activation values across all neurons in all layers."""
    return activations.min().item(), activations.max().item()

def compute_histogram(activations, global_min, global_max, bins=1000):
    """Compute histogram with global min and max to ensure consistent bin sizes."""
    hist = torch.histc(activations, bins=bins, min=global_min, max=global_max)
    return hist

def estimate_peaks_with_layer_prior(activations, bins=1000):
    """Estimate neuron peaks using the entire layer's distribution as a prior."""
    global_min, global_max = get_global_min_max(activations)
    peak_values = torch.empty_like(activations[:, :, 0])  # Assuming activations shape is [layers, neurons, activations]

    for i in range(activations.size()[0]):  # Iterating over layers
        # Compute the layer-wide histogram with consistent bin edges
        layer_activations = activations[i].reshape(-1)  # Flatten layer activations
        layer_hist = compute_histogram(layer_activations, global_min, global_max, bins)

        for j in range(activations.size()[1]):  # Iterating over neurons in the layer
            neuron_activations = activations[i, j]
            # Compute neuron-specific histogram with the same global min/max and bins
            neuron_hist = compute_histogram(neuron_activations, global_min, global_max, bins)
            # Directly use neuron_hist for peak estimation if layer-wide information isn't strictly necessary
            # Or adjust neuron_hist using layer-wide information as previously intended
            peak_bin = neuron_hist.argmax()
            bin_width = (global_max - global_min) / bins
            peak_value = global_min + bin_width * (peak_bin.float() + 0.5)
            peak_values[i, j] = peak_value

    return peak_values




def get_kde_peaks(activations, bandwidth=0.1):
    layers, neurons, _ = activations.shape  # Assuming activations is a 3D tensor of shape [layers, neurons, activations]

    # Initialize an empty tensor for main peak values with shape [layers, neurons]
    main_peak_values = torch.empty((layers, neurons), dtype=torch.float32)

    # Ensure activations are in float32 for KDE
    if activations.dtype == torch.float16:
        activations_float32 = activations.float()
    else:
        activations_float32 = activations

    # Iterate over each layer and neuron to compute the main peak value
    for layer in range(layers):
        print(f"Calculating KDE for layer {layer+1} of {layers}")
        for neuron in range(neurons):
            # Convert activations to numpy for KDE computation
            activations_np = activations_float32[layer, neuron].cpu().numpy().flatten()

            # Perform Kernel Density Estimation
            kde = stats.gaussian_kde(activations_np, bw_method=bandwidth)
            
            # Evaluate the KDE on a fine grid to find the peak
            grid = np.linspace(activations_np.min(), activations_np.max(), 1000)
            kde_values = kde.evaluate(grid)
            
            # Identify the main peak as the grid value with the highest KDE estimate
            main_peak_value = grid[np.argmax(kde_values)]

            # Store the main peak value
            main_peak_values[layer, neuron] = main_peak_value

    # No need to adjust activations here; just return the 2D tensor of main peak values
    return main_peak_values

def get_mode_offsets(activations):
    # Check if the activations tensor is of type torch.float16
    if activations.dtype == torch.float16:
        # Convert to torch.float32 for mode calculation
        activations_float32 = activations.float()
    else:
        # Use the original tensor if it's already in a supported data type
        activations_float32 = activations

    # Compute the mode across the last dimension for each neuron in every layer
    mode_values_float32, _ = torch.mode(activations_float32, dim=-1)
    #mode_values_float32 = torch.mean(activations_float32, dim=-1)
    
    # If the original tensor was torch.float16, convert the result back to torch.float16
    if activations.dtype == torch.float16:
        mode_values = mode_values_float32.half()
    else:
        mode_values = mode_values_float32
    
    # The mode_values tensor will have shape [layers, neurons], which is already 2D
    # and matches the requirement of returning a 2D tensor of mode values.
    
    #flip the sign on everything since we are doing addition in the offset mask
    #return mode_values * -1
    return mode_values

def score_and_prune( opt: Model,
            focus_activations_data: ActivationOverview,
            cripple_activations_data: ActivationOverview,
            pruning_config: PruningConfig,
            save=False,
        ):
    # Get the top fraction FF activations and prune
    ff_frac, ff_eps     = pruning_config.ff_frac,   pruning_config.ff_eps
    attn_frac, attn_eps = pruning_config.attn_frac, pruning_config.attn_eps
    do_ff   = ff_frac > 0
    do_attn = attn_frac > 0

    act_subset = pruning_config.scoring_normalization
    if do_ff > 0:
        ff_focus_data   = focus_activations_data.ff[act_subset]
        ff_cripple_data = cripple_activations_data.ff[act_subset]
        ff_scoring_fn = score_indices_by(pruning_config.ff_scoring)

        ff_scores = ff_scoring_fn(opt, ff_focus_data, ff_cripple_data, ff_eps)
        ff_criteria, ff_threshold = get_top_frac(ff_scores, ff_frac)
        opt.delete_ff_keys(ff_criteria)

    # Get the top fraction of Attention activations and prune
    if do_attn > 0:
        attn_focus_data   = focus_activations_data.attn[act_subset]
        attn_cripple_data = cripple_activations_data.attn[act_subset]
        # scoring for attention
        attn_scoring_fn = score_indices_by(pruning_config.attn_scoring)
        attn_scores = attn_scoring_fn(opt, attn_focus_data, attn_cripple_data, attn_eps)

        # offset by means if desired (probably bad?)
        means = None
        if pruning_config.do_attn_mean_offset:
            means = attn_focus_data["mean"]

        # get criteria for "neurons", or for "heads" if using full heads
        if pruning_config.attn_prune_heads:
            attn_head_scoring_fn = \
                choose_attn_heads_by(pruning_config.attn_prune_heads_mode)
            attn_criteria, attn_threshold = \
                attn_head_scoring_fn(opt, attn_scores, attn_frac)
            attn_criteria = opt.expand_remove_heads_to_remove_indices(attn_criteria)
        else:
            attn_criteria, attn_threshold = get_top_frac(attn_scores, attn_frac)
            _shape = (opt.cfg.n_layers, opt.cfg.n_heads, opt.cfg.d_head)
            attn_criteria = attn_criteria.reshape(_shape)

        # get criteria and prune if using only attention neurons
        if pruning_config.attn_mode == "pre-out":
            opt.delete_attn_pre_out( attn_criteria, means )
        elif pruning_config.attn_mode == "value":
            opt.delete_attn_values( attn_criteria, means )
        else:
            raise NotImplementedError("attn_mode must be 'pre-out' or 'value'")

    # Save the removals to file
    tensor_data = {
        "ff_scores": ff_scores if do_ff else None,
        # FIXME: doesn't return attn_std_mean
        "attn_scores": attn_scores if do_attn else None,
        "ff_criteria": ff_criteria if do_ff else None,
        "attn_criteria": attn_criteria if do_attn else None,
    }
    if save:
        save_timestamped_tensor_dict( opt, tensor_data, "activation_metrics" )

    # Initialize the output dictionary
    data = RunDataItem()

    data.update({'deletions': {
        "ff_threshold": ff_threshold if do_ff else 0,
        "attn_threshold": attn_threshold if do_attn else 0,
        "ff_del": float( torch.sum(ff_criteria) ) if do_ff else 0,
        "attn_del": float( torch.sum(attn_criteria) ) if do_attn else 0,
    }})

    data.update({'deletions_per_layer': {
        'ff': ff_criteria.sum(dim=-1).tolist() if do_ff else [],
        'attn': attn_criteria.sum(dim=-1).tolist() if do_attn else [],
    }})

    # Save removals and scores to history
    _numpify = lambda x: x.cpu().numpy() if x is not None else None
    data.update({'raw': {
        k: _numpify(v) for k,v in tensor_data.items()
    }})

    return data

def prune_random( opt: Model,
        ff_frac: float,
        attn_frac: float,
        ff_pruned: Optional[np.ndarray] = None,
        attn_pruned: Optional[np.ndarray] = None,
        ):
    """Prune a random fraction of FF and Attention weights
    Args:
        opt (Model): model to prune and evaluate
        ff_frac (float): fraction of FF to prune
        attn_frac (float): fraction of Attention to prune

    """
    if ff_pruned is None:
        ff_pruned = np.zeros( (opt.cfg.n_layers, opt.cfg.d_mlp), dtype=np.bool_ )
    if attn_pruned is None:
        attn_pruned = np.zeros( (opt.cfg.n_layers, opt.cfg.d_model ), dtype=np.bool_ )

    n_ff_to_prune   = int( ff_frac   * opt.cfg.d_mlp )
    n_attn_to_prune = int( attn_frac * opt.cfg.d_model )

    # First prune the FF
    if not ff_frac == 0:
        for layer in range( opt.cfg.n_layers ):
            # choose new ff neurons to prune
            indices = np.where(ff_pruned[layer] == 0)[0]
            random_indices = np.random.choice(indices, n_ff_to_prune, replace=False)
            ff_pruned[layer][random_indices] = 1

        # Prune the model
        opt.delete_ff_keys( ff_pruned )

    if not attn_frac == 0:
        for layer in range( opt.cfg.n_layers ):
            # choose new attention heads to prune
            indices = np.where(attn_pruned[layer] == 0)[0]
            random_indices = np.random.choice(indices, n_attn_to_prune, replace=False)
            attn_pruned[layer][random_indices] = 1

        # Prune the model
        opt.delete_attn_pre_out( attn_pruned )

    data_out = {
        "ff_del": n_ff_to_prune*opt.cfg.n_layers,
        "attn_del": n_attn_to_prune*opt.cfg.n_layers
    }
    return ff_pruned, attn_pruned, data_out

def prune_random_and_evaluate( opt: Model,
        c: PruningConfig,
        ff_pruned: Optional[np.ndarray] = None,
        attn_pruned: Optional[np.ndarray] = None,
        ):
    """
    To use, run once with ff_pruned=None and attn_pruned=None, then run again
    with the parameters given as output passed back in.

    Args:
        opt (Model): The model to prune and evaluate
        c (PruningConfig): The pruning configuration
        ff_pruned (Optional[np.ndarray]): Bool list of FF neurons, default None.
        attn_pruned (Optional[np.ndarray], optional: Bool list of ATTN neurons, default None.

    Returns:
        ff_pruned (Optional[np.ndarray]):
        attn_pruned (Optional[np.ndarray]):
        data (RunDataItem):
    """


    # Prune the model randomly
    ff_pruned, attn_pruned, data_out = \
        prune_random( opt, c.ff_frac, c.attn_frac, ff_pruned, attn_pruned )

    # Initialize the output dictionary
    data = RunDataItem()

    # Evaluate the model
    # TODO: return to normal c.datasets
    data.update(
        evaluate_all( opt, c.eval_sample_size, [c.focus],
                      dataset_tokens_to_skip=c.collection_sample_size )
    )
    #data.update(
    #    evaluate_all( opt, c.eval_sample_size, c.datasets,
    #                  dataset_tokens_to_skip=c.collection_sample_size )
    #)

    data.update({'deletions': data_out })

    data.update({'deletions_per_layer': {
        'ff': ff_pruned.sum(axis=-1).tolist() if (not ff_pruned is None) else 0,
        'attn': attn_pruned.sum(axis=-1).tolist() if (not attn_pruned is None) else 0,
    }})

    return ff_pruned, attn_pruned, data

######################################################################################
# Run Whole Pruning Procedure from Config
######################################################################################

def run_pruning(c: PruningConfig):
    #TODO: delete this
    print("Starting pruning in ben's _____TAKER____")
    print(c)
    # Initilaise Model and show details about model
    opt = Model(
        c.model_size,
        limit=c.token_limit,
        dtype=c.dtype,
        svd_attn=c.svd_attn,
        use_accelerator=c.use_accelerator,
        model_device=c.model_device,
        mask_fn=c.mask_fn,
        )

    # Prepare data logging
    history = RunDataHistory(c.datasets)
    wandb.init(
        project=c.wandb_project,
        entity=c.wandb_entity,
        name=c.wandb_run_name,
        )
    wandb.config.update(c.to_dict())

    # Evaluate model before removal of any neurons
    if c.run_pre_test:
        data = evaluate_all(opt, c.eval_sample_size,
            [c.focus], c.collection_sample_size)
        history.add(data)
        print(history.df.T)

    # If pruning randomly, no need to get activations
    if c.ff_scoring == "random" and c.attn_scoring == "random":
        if c.attn_offset_mode != "zero":
            do_collect_raw_attn = (c.attn_offset_mode == "peak")
            focus_out   = get_midlayer_activations(opt, c.focus,
                            c.collection_sample_size, c.attn_mode, collect_attn=do_collect_raw_attn)

        #cripple_out = get_midlayer_activations(opt, c.cripple,
        #                c.collection_sample_size, c.attn_mode, collect_attn=True) #TODO: is collect attn needed?

        #print(f"size of raw_attn_activations: {raw_attn_activations.size()}")
        #make offset tensor layer x d_model of zeros
        print("Starting to calculate offsets for random scoring...")
        if c.attn_offset_mode == "peak":
            raw_attn_activations = focus_out.raw["attn"].permute( (1,2,3,0) ).reshape( (opt.cfg.n_layers, opt.cfg.d_model, -1) )
            #print("!!!not calculating offsets for this random run!!!")
            #offsets = get_kde_peaks(raw_attn_activations)
            #torch.save(offsets, "offsets-kde-focus-opt-1.2b.pt")
            #print("Saved Offsets")

            #TODO: do I need to make everything negative in offsets first?
            offsets = get_mean_offsets(raw_attn_activations)
            opt.update_mask_offsets("attn_pre_out", offsets)
            print("finished calculating offsets for random scoring!")
        
        if c.attn_offset_mode == "mean":
            print("!!doing mean offset") #TODO: delete
            offsets = einops.rearrange(focus_out.attn.orig.mean,
                "n_layers n_heads d_head -> n_layers (n_heads d_head)")
            print(offsets.shape)
            opt.update_mask_offsets("attn_pre_out", offsets)
            print("finished calculating offsets for random scoring!")

        ff_pruned, attn_pruned = None, None
        for i in range(c.n_steps):
            ff_pruned, attn_pruned, data = \
                prune_random_and_evaluate(opt, c, ff_pruned, attn_pruned)
            history.add(data)

    # Iteratively prune neurons and evaluate
    elif c.recalculate_activations:
        focus_out   = get_midlayer_activations(opt, c.focus,
                        c.collection_sample_size, c.attn_mode, collect_attn=True)
        #cripple_out = get_midlayer_activations(opt, c.cripple,
        #                c.collection_sample_size, c.attn_mode)
        cripple_out = get_midlayer_activations(opt, c.cripple,
                        c.collection_sample_size, c.attn_mode, collect_attn=True) #TODO: is collect attn needed?

        raw_attn_activations = focus_out.raw["attn"].permute( (1,2,3,0) ).reshape( (opt.cfg.n_layers, opt.cfg.d_model, -1) )
        #print(f"size of raw_attn_activations: {raw_attn_activations.size()}")
        #make offset tensor layer x d_model of zeros
        offsets = get_mode_offsets(raw_attn_activations)
        #load offsets from file
        #offsets = torch.load("offsets-kde-focus-opt-125m.pt")
        
        print("Starting to calculate offsets w/ recalc activations...")

        #TODO: do I need to make everything negative in offsets first?
        opt.update_mask_offsets("attn_pre_out", offsets)
        print("finished calculating offsets w/ recalc activations!")
        for _ in range(c.n_steps):
            data = prune_and_evaluate(opt, c)
            history.add(data)

    # Non-iteratively get activations, then iteratively prune and evaluate
    else:
        focus_out   = get_midlayer_activations(opt, c.focus,
                        c.collection_sample_size, c.attn_mode, collect_attn=True)
        #cripple_out = get_midlayer_activations(opt, c.cripple,
        #                c.collection_sample_size, c.attn_mode)
        cripple_out = get_midlayer_activations(opt, c.cripple,
                        c.collection_sample_size, c.attn_mode, collect_attn=True) #TODO: is collect attn needed?

        raw_attn_activations = focus_out.raw["attn"].permute( (1,2,3,0) ).reshape( (opt.cfg.n_layers, opt.cfg.d_model, -1) )
        #print(f"size of raw_attn_activations: {raw_attn_activations.size()}")
        #make offset tensor layer x d_model of zeros
        offsets = get_mode_offsets(raw_attn_activations)
        print("Starting to calculate offsets non-iteratively getting activations...")

        #TODO: do I need to make everything negative in offsets first?
        opt.update_mask_offsets("attn_pre_out", offsets)
        print("finished calculating offsets non-iteratively getting activations!")
        for i in range(c.n_steps):
            data = prune_and_evaluate(opt, c, focus_out, cripple_out, i)
            history.add(data)

    # Format history to print
    print(history.history[-1])
    print(history.df.T)
    print(history.df.T.to_csv())

    return opt, history

######################################################################################
# "Forsaken"-style pruning
######################################################################################

def forsaken_pruning(c: PruningConfig,
        num_texts: int = 1,
        lr: float = 0.1,
        sigmoid_offset: float = 2.0,
        l1_norm_coeff: float = 1.0,
        ):
    # Initilaise Model and show details about model
    c.mask_fn = "sigmoid"
    c.misc = {
        "num_texts": num_texts,
        "lr": lr,
        "sigmoid_offset": sigmoid_offset,
        "l1_norm_coeff": l1_norm_coeff,
    }

    opt = Model(
        c.model_size,
        limit=c.token_limit,
        dtype=c.dtype,
        svd_attn=c.svd_attn,
        use_accelerator=c.use_accelerator,
        model_device=c.model_device,
        mask_fn=c.mask_fn,
        )

    # Prepare data logging
    history = RunDataHistory(c.datasets)
    wandb.init(
        project=c.wandb_project,
        entity=c.wandb_entity,
        name=c.wandb_run_name,
        )
    wandb.config.update(c.to_dict())

    # Evaluate model before removal of any neurons
    if c.run_pre_test:
        data = evaluate_all(opt, c.eval_sample_size,
            c.datasets, c.collection_sample_size)
        history.add(data)
        print(history.df.T)

    # Get activations
    focus_out   = get_midlayer_activations(opt, c.focus,
                    c.collection_sample_size, c.attn_mode)
    cripple_out = get_midlayer_activations(opt, c.cripple,
                    c.collection_sample_size, c.attn_mode)

    def normalize_scores(scores):
        normed_scores = []
        for score in scores:
            normed_scores.append(
                (score - score.mean()) / score.std()
            )
        return torch.stack(normed_scores)

    # Set masks for feed-forward layers
    ff_scores   = score_indices(c.ff_scoring,
        opt, focus_out.ff.orig,   cripple_out.ff.orig)
    ff_masks    = sigmoid_offset - normalize_scores(ff_scores)
    for layer_index in range(opt.cfg.n_layers):
        mask = opt.masks["mlp_pre_out"][layer_index]
        mask.set_mask(ff_masks[layer_index])

    # Set masks for attention heads
    attn_scores = score_indices(c.attn_scoring,
        opt, focus_out.attn.orig, cripple_out.attn.orig)
    attn_masks  = sigmoid_offset - normalize_scores(attn_scores)
    for layer_index in range(opt.cfg.n_layers):
        mask = opt.masks["attn_pre_out"][layer_index]
        mask.set_mask(attn_masks[layer_index])

    # Evaluate again now that we have adjusted the masks
    if True:
        data = evaluate_all(opt, c.eval_sample_size,
            c.datasets, c.collection_sample_size)
        history.add(data)

    # Get parameters for back propagation
    mask_params = [
        *[p for mask in opt.masks["mlp_pre_out"] for p in mask.parameters()],
        *[p for mask in opt.masks["attn_pre_out"] for p in mask.parameters()],
    ]
    mask_l1_norm = torch.stack([
        *[(1-mask.get_mask()).mean() for mask in opt.masks["mlp_pre_out"]],
        *[(1-mask.get_mask()).mean() for mask in opt.masks["attn_pre_out"]],
    ]).mean()

    # Generate Inputs
    n_iter = 4
    optim = torch.optim.LBFGS(mask_params, lr, max_iter=n_iter)
    kl_loss_fn = torch.nn.KLDivLoss()
    #ce_loss_fn = torch.nn.CrossEntropyLoss()

    # Load datasets
    def gen_texts(num_texts=1):
        _cripple_texts, _focus_texts = [], []

        cripple_dataset, cripple_label, _skip50 = prepare(c.cripple)
        i = 0
        for data in cripple_dataset:
            i += 1
            if i > num_texts:
                break
            _cripple_texts.append(data[cripple_label])

        focus_dataset, focus_label, _skip50     = prepare(c.focus)
        i = 0
        for data in focus_dataset:
            i += 1
            if i > num_texts:
                break
            _focus_texts.append(data[focus_label])

        return _cripple_texts, _focus_texts

    # Begin calculating loss for with LBGFS
    def get_new_ids(n_batches = None):
        batches = []
        cripple_texts, focus_texts = gen_texts()
        bad_ids, junk_ids, good_ids = [], [], []
        with torch.no_grad():
            for text in cripple_texts:
                bad_ids.append( opt.get_ids(text) )
                junk_ids.append(
                    torch.randint_like(bad_ids[-1], 5, opt.tokenizer.vocab_size)
                )
            for text in focus_texts:
                good_ids.append( opt.get_ids(text) )
        return bad_ids, junk_ids, good_ids

    for j in range(c.n_steps//n_iter):
        bad_ids, junk_ids, good_ids = get_new_ids()

        # Begin LBGFS
        def closure():
            loss = 0
            optim.zero_grad()

            # Generate loss
            loss += mask_l1_norm * l1_norm_coeff

            for i in range(num_texts):
                # Get junk loss L_kl(gamma,P)
                with torch.no_grad():
                    junk_logits = opt.get_all_logits(junk_ids[i])[..., :-1, :]
                bad_logits = opt.get_all_logits(bad_ids[i])[..., :-1, :]
                loss += kl_loss_fn(bad_logits, junk_logits).mean()

                # Get good loss L_kl(gamma,Q)
                with torch.no_grad():
                    opt.masking_enabled = False
                    orig_logits = opt.get_all_logits(good_ids[i])[..., :-1, :]
                    opt.masking_enabled = True
                new_logits = opt.get_all_logits(good_ids[i])[..., :-1, :]
                loss += kl_loss_fn(new_logits, orig_logits).mean()

            # Backpropagate
            loss.backward(retain_graph=True)
            return loss

        # loss step
        for i in range(n_iter):
            optim.step(closure)
            data = evaluate_all(opt, c.eval_sample_size,
                c.datasets, c.collection_sample_size)
            history.add(data)

    # Format history to print
    print(history.history[-1])
    print(history.df.T)
    print(history.df.T.to_csv())

    return opt, history

