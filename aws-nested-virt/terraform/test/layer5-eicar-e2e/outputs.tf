output "instance_id" {
  description = "Test host instance ID — driver script polls SSM against this for the bootstrap-ready marker, then orchestrates EICAR submission."
  value       = aws_instance.test.id
  # Don't surface the ID until the post-nested-virt restart is complete;
  # the driver's SSM polling otherwise races with the stop/start cycle.
  depends_on = [terraform_data.enable_nested_virtualization]
}

output "public_ip" {
  description = "Public IP for SSH/curl debugging if needed."
  value       = aws_instance.test.public_ip
}

output "result_marker_path" {
  description = "Path on the host where userdata writes the bootstrap pass/fail JSON."
  value       = "/var/log/layer5-bootstrap-result.json"
}

output "eicar_result_path" {
  description = "Path on the host where the EICAR driver script writes the analysis pass/fail JSON."
  value       = "/var/log/layer5-eicar-result.json"
}
